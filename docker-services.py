#!/usr/bin/env python3

"""
Copyright (c) 2015  Jonas Thiem

This software is provided 'as-is', without any express or implied
warranty. In no event will the authors be held liable for any damages
arising from the use of this software.

Permission is granted to anyone to use this software for any purpose,
including commercial applications, and to alter it and redistribute it
freely, subject to the following restrictions:

1. The origin of this software must not be misrepresented; you must not
   claim that you wrote the original software. If you use this software
   in a product, an acknowledgement in the product documentation would be
   appreciated but is not required.
2. Altered source versions must be plainly marked as such, and must not be
   misrepresented as being the original software.
3. This notice may not be removed or altered from any source distribution.
"""

"""
    # What is this

    ## Motivation

    You have multiple complex services, each consisting of 1+ Dockerfiles
    and its docker-compose.yml which launches it. You suddenly discover you
    need a nice way to launch and manage all of these services in one go!


    ## Features

    - easily start/stop and manage multiple docker-compose controlled services

    - clean up all stopped containers in one go (useful if you rely purely on
      images, docker-compose usually creates new containers when launching)

    - creation of atomic snapshots of all your read-write volumes/live data
      without shutting down or pausing your containers (for backup purposes)

    - one self-contained file



    # Basic usage

    Usage:

    ```
      docker-services.py list                - list all known services
      docker-services.py start <service>     - starts the specified service
      docker-services.py stop <service>      - stops the specified service
      docker-services.py restart <service>   - restarts the given service      
      docker-services.py logs <service>      - print logs of all docker 
                                               containers of the service
      docker-services.py shell <service> <subservice>  - run a shell in the
                                                         specified subservice.
      docker-services.py snapshot <service>  - makes a snapshot of the live
                                               data if enabled (optional)
      docker-services.py clean               - deletes all containers that
                                               aren't running (IRREVOCABLE)
    ```

    **Hint**: You can always use "all" as service target.



    # Installation

    Copy docker-services.py to /usr/bin/ and set execution bit (chmod +x)



    # HOW TO add your service

    Services are organized in subfolders. The script will scan the following
    locations for services folders:
    
    - the current working directory when running the script
    - ~/.docker-services/
    - /usr/share/docker-services/  
 
    Each service folder should contain:

    - Dockerfile(s) + required misc data. (possibly in subfolders as needed)
    - livedata/ folder for all read-write volumes (recommended, see snapshots
                                                   as described below)
    - docker-compose.yml to launch it. (folders without this file are skipped)

      (why the docker-compose.yml? Because your service most likely requires
      persistant volumes mounted and ports forwarded, which a Dockerfile can't
      specify in detail by design)

    Congratulations, you can now launch your service(s)!



    # HOW TO backup

    You should backup all your services. This will be easy since you can
    simply copy your whole services folder.
   
    However, to get your live data (SQL databases etc.) backed up properly
    during operations, make sure to:

    1. Enable snapshots as described below

    2. Always run "docker-services.py snapshot all" before you make your
       backup.



    # HOW TO enable snapshots (optional)

    This feature allows you to easily copy all the live data of your read-write
    mounted volumes your containers as atomic snapshots even while your
    services are running and continue to write data.

    The snapshots will be atomic, therefore suitable even for database realtime
    operations while the database is running and writing to the volume(s).

    ## Enable it for a service

    How to enable snapshots for a service:

    1. Make sure your services folder is on a btrfs file system (not ext4).

    2. Each of your snapshot enabled services needs to have a subfolder
       livedata/ where all read-write volumes of it are located.

    ## Test/do it

    **Before you go into production, make sure to test snapshots for your
    service(s) at least once!**

    Calling:
       ``` docker-services.py snapshot <service>|"all" ```
  
    will now use btrfs functionality to add a time-stamped folder with an
    atomic snapshot of livedata/ of the specified service(s) into a new
    livedata-snapshots/ subfolder - while your service can continue using the
    volume thanks to btrfs' copy-on-write snapshot functionality.

    ## Restore a snapshot

    You can easily restore such a snapshot by shutting down your service
    temporarily, copying back a snapshot into livedata/ and turning your
    service back on.

"""

""" Copyright (C) Jonas Thiem, 2015
"""

import argparse
from argparse import RawTextHelpFormatter, HelpFormatter
import datetime
import json
import os
import subprocess
import re
import shutil
import sys
import textwrap
import threading
import time
import uuid

# our list of known services:
services = []

class DoubleLineBreakFormatter(HelpFormatter):
    """ Retains double line breaks/paragraphs """
    def _split_lines(self, text, width):
        return self._fill_text(text, width, "").splitlines(False)

    def _fill_text(self, t, width, indent):
        t = " ".join([s for s in t.replace("\t", " ").strip("\t ").split(" ")\
            if len(s) > 0]).replace("\n ", "\n").replace(" \n", " ")
        ts = re.sub("([^\n])\n([^\n])", "\\1 \\2", t).split("\n\n")
        result = [textwrap.fill(paragraph, width,
            initial_indent=indent, subsequent_indent=indent)\
            for paragraph in ts]
        return "\n\n".join(result)

def locate_binary(name):
    """ Locate a binary of some name, or return None if it can't be found.
    """
    badchars = '\'"$<> %|&():*/\\{}#!?=\n\r\t[]\033'
    for char in badchars:
        if name.find(char) >= 0:
            raise ValueError("dangerous character in binary name")
    output = None
    try:
        output = subprocess.check_output("which " + name, shell=True,
            stderr=subprocess.STDOUT).\
            decode("utf-8", "ignore").strip()
    except subprocess.CalledProcessError:
        pass
    if output == None or len(output) == 0:
        return None
    return output

cached_docker_path = None
def docker_path():
    """ Locate docker binary and return its path, or exit process with error
        if not available.
    """
    global cached_docker_path
    if cached_docker_path != None:
        return cached_docker_path
    
    def behaves_like_docker(binary_path):
        output = None
        try:
            output = subprocess.check_output([binary_path, "--version"],
                stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            output = e.output
        output = output.decode("utf-8", "ignore")
        return (output.lower().startswith("docker version "))

    test_names = [ "docker.io", "docker" ]

    for test_name in test_names:
        bin_path = locate_binary(test_name)
        if bin_path == None:
            continue
        if behaves_like_docker(bin_path):
            return bin_path
    print("docker-services.py: error: no docker found. Is it installed?")
    sys.exit(1)

def docker_compose_path():
    """ Locate docker-compose binary and return its path, or exit process with
        error if not available.
    """
    bin_path = locate_binary("docker-compose")
    if bin_path != None:
        return bin_path
    print("docker-services.py: error: no docker-compose found. " + \
        "Is it installed?")
    sys.exit(1)

def btrfs_path():
    """ Locate btrfs helper tool binary and return its path, or return None if
        not found.
    """
    bin_path = locate_binary("docker-compose")
    if bin_path != None:
        return bin_path
    return None

def filesystem_of_path(path):
    """ Find out the filesystem a given directory or file is on and return
        the name (e.g. "ext4", "btrfs", ...)
    """
    if not os.path.exists(path):
        raise ValueError("given path does not exist: " + str(path))
    output = subprocess.check_output([locate_binary("df"), path]).\
        decode("utf-8", "ignore")

    # skip first line:
    if output.find("\n") <= 0:
        raise RuntimeError("failed to parse df output")
    output = output[output.find("\n")+1:]

    # get first word being the FS of the path:
    end_pos = output.find(" ")
    if end_pos <= 0:
        raise RuntimeError("failed to parse df output")
    device_of_path = output[:end_pos]
    if device_of_path == "-":
        # we can't find out the filesystem of this path.
        # -> try to find out the parent!
        return filesystem_of_path(os.path.normpath(path + "/../"))

    output = subprocess.check_output([locate_binary("mount")]).\
        decode("utf-8", "ignore")
    for line in output.split("\n"):
        if not line.startswith(device_of_path + " "):
            continue
        i = len(line) - 1
        while not line[i:].startswith(" type ") and i > 0:
            i -= 1
        if not line[i:].startswith(" type "):
            raise RuntimeError("failed to parse mount output")
        fs_type = line[i + len(" type "):].strip()
        if fs_type.find(" ") > 0:
            fs_type = fs_type[:fs_type.find(" ")].strip()
        return fs_type
    raise RuntimeError('failed to find according mount entry')

def print_msg(text, service=None, color="blue"):
    """ Print out a nicely formatted message prefixed with
        [docker-services.py] and possibly a service name.
    """
    def color_code():
        part = "\033[1;"
        if color == "blue":
            part += "34"
        elif color == "red":
            part += "31"
        elif color == "yellow":
            part += "33"
        elif color == "green":
            part += "32"
        elif color == "white":
            part += "37"
        return part + "m"

    service_part = ""
    if service != None and len(service) > 0:
        service_part = "\033[0m/" + color_code() + service

    docker_services_part = "docker-services.py"
    if service == None or len(service) == 0 or True:
        docker_services_part = color_code() + "docker-services.py"

    initial_length = len("[docker-services.py")
    if service != None:
        initial_length += len("/" + service)
    initial_length += len("] ") 

    print("\033[0m\033[1m[\033[0m" + docker_services_part + \
        service_part + "\033[0m\033[1m] \033[0m" + \
        textwrap.fill(text, 79,
            initial_indent=(" " * initial_length),
            subsequent_indent=(" " * initial_length))[
            initial_length:] + \
        "\033[0m")

def get_external_link_references_from_compose_yml(service_path, service_name):
    """ Attempt to parse and return all containers referenced as external
        links used by a service from the respective docker-compose.yml of that
        service.
    """
    external_links = []
    f = open(os.path.join(service_path, "docker-compose.yml"), "rb")
    contents = f.read().decode("utf-8", "ignore").replace("\r\n", "\n").\
        replace("\r", "\n").split("\n")
    in_external_links_list = False
    for line in contents:
        if line.strip() == "":
            continue
        if line.startswith(" ") and (line.strip().startswith("external_links ")
                or line.strip().startswith("external_links:")):
            in_external_links_list = True

            # parse remaining stuff in line:
            i = line.find("external_links")
            line = line[i+len("external_links"):].strip()
            if line.startswith(":"):
                line = line[1:].strip()
            if len(line) == 0:
                continue

        if in_external_links_list:
            if not line.strip().startswith("-"):
                in_external_links_list = False
                continue
            line = line[line.find("-")+1:].strip()
            parts = line.split(":")
            
            external_links.append(parts[0])
    f.close()
    return external_links

def get_service_container_names(service_path, service_name):
    """ Get the label names of all containers specified for the given service,
        no matter if running or even created at the moment or not.

        Those names are what the most recently created current container of
        each service is named like. If the service has no container yet, it
        will still have the future name returned in this listing.
    """
    f = open(os.path.join(service_path, "docker-compose.yml"), "rb")
    contents = f.read().decode("utf-8", "ignore").replace("\r\n", "\n").\
        replace("\r", "\n").replace("\t", " ").split("\n")
    results = []
    smallest_observed_indentation = 999
    def count_indentation(line):
        i = 0
        while i < len(line) and line[i] == " ":
            i += 1
        return i
    current_service_name = None
    i = 0
    for line in contents:
        next_line = None
        i += 1
        if i < len(contents):
            next_line = contents[i]

        if not (line.startswith(" ")):
            # this is the service name:
            current_service_name = line.partition(":")[0].strip().\
                partition(" ")[0]
        elif count_indentation(line) < smallest_observed_indentation:
            # this is a line inside the service declaration.
            keyword = line.partition(":")[0].strip().partition(" ")[0]
            value = line.partition(":")[2].strip()
            if len(value) == 0:
                value = next_line.strip()
            if keyword == "build":
                # this service build from a directory. get name of directory:
                build_path = os.path.normpath(\
                    os.path.join(service_path, value))
                while build_path.endswith("/"):
                    build_path = build_path[:-1]
                build_name = os.path.basename(build_path).replace("-", "")
                
                # this is the resulting container name:
                results.append(build_name + "_" +\
                    current_service_name + "_1")
            elif keyword == "image":
                # this container is constructed from an image:
                image_name = value

                # this is the resulting container name:
                results.append(current_service_name + "_" + \
                    build_name + "_1")
    return results

def get_service_dependencies(service_path, service_name):
    global services
    # collect all external_links container references:
    deps = get_external_link_references_from_compose_yml(service_path,
        service_name)
    dependent_services = []
    if len(deps) > 0:
        # find out which other service owns the container and add it:
        for dep in deps:
            for service in services:
                service_container_names = get_service_container_names(
                    service["folder"], service["name"])
                if dep in service_container_names:
                    dependent_services.append({
                        "folder" : service["folder"],\
                        "name" : service["name"]
                    })
    return dependent_services

launch_completed = []
launch_threads = []

class LaunchThreaded(threading.Thread):
    """ A helper to launch a service and wait for the launch only for a
        limited amount of time, and moving the launch into a background
        thread if it takes too long.
    """
    def __init__(self, service_dir, service_name):
        super().__init__()
        self.name = service_name
        self.path = service_dir

    def run(self):
        global launch_completed
        try:
            # Fix permissions if we have instructions for that:
            perm_info = get_permission_info_from_yml(self.path, self.name)
            if ("owner" in perm_info["livedata-permissions"]) \
                    and os.path.exists(os.path.join(self.path, "livedata")):
                print_msg("ensuring file permissions of livedata folder...",\
                    service=self.name, color="blue")
                owner = perm_info["livedata-permissions"]["owner"]
                try:
                    owner = int(owner)
                except TypeError:
                    # must be a name.
                    try:
                        owner = getpwnam(owner).pw_uid
                    except KeyError:
                        print_msg("invalid user specified for permissions: "+\
                            "can't get uid for user: " + owner, color="red")
                        raise RuntimeError("invalid user")
                for root, dirs, files in os.walk(os.path.join(self.path, \
                        "livedata")):
                    for f in (dirs + files):
                        fpath = os.path.join(root, f)
                        os.chown(fpath, owner, -1, follow_symlinks=False)

            # Get dependencies and see if they have all been launched:
            dependencies = get_service_dependencies(
                self.path, self.name)
            waiting_msg = False
            for dependency in dependencies:
                if not dependency in launch_completed:
                    if not waiting_msg:
                        waiting_msg = True
                        print_msg("waiting for dependency to launch: " +\
                            str(dependency), service=self.name,
                            color="yellow")
                    while (not dependency in launch_completed):
                        time.sleep(1)

            # Launch the service:
            print_msg("launching...", service=self.name, color="blue")
            try:
                subprocess.check_call([docker_compose_path(), "rm", "-f"],
                    cwd=self.path)
                subprocess.check_call([docker_compose_path(), "build"],
                    cwd=self.path)
                subprocess.check_call([docker_compose_path(), "up", "-d"],
                    cwd=self.path)
                time.sleep(1)
                if not is_service_running(self.path, self.name):
                    print_msg("failed to launch. (nothing running after " +\
                        "1 second)",\
                        service=self.name, color="red")
                    return
                launch_completed.append({"folder":self.path, "name":self.name})
                print_msg("now running.", service=self.name, color="green")
            except subprocess.CalledProcessError:
                launch_completed.append({"folder":self.path, "name":self.name})
                print_msg("failed to launch. (error exit code)",\
                    service=self.name, color="red")
        except Exception as e:
            print("UNEXPECTED ERROR", file=sys.stderr)
            print("ERROR: " + str(e))

    @staticmethod
    def attempt_launch(directory, service, to_background_timeout=5):
        """ Launch a given service and wait for it to run for a few seconds.
            If that isn't long enough for it to start running, return
            execution to possibly launch further services while this one is
            still busy launching.
        """
        global launch_threads

        # Start a new launch thread:
        launch_t = LaunchThreaded(directory, service)
        launch_t.start()
        
        # Wait for it to complete:
        launch_t.join(to_background_timeout)
        if launch_t.isAlive():
            # This took too long, run in background:
            print_msg("Maximum waiting time exceeded, " + \
                "resuming launch in background.", service=service,
                color="yellow")
            launch_threads.append(launch_t)

    @staticmethod
    def stop(directory, service):
        """ Stop a service. """
        subprocess.check_call([docker_compose_path(), "stop"],
            cwd=directory)

    @staticmethod
    def wait_for_launches():
        global launch_threads
        for launch_t in launch_threads:
            if launch_t.isAlive():
                launch_t.join()

def get_existing_container_volumes(container_id, rw_only=False):
    """ Get a list of all volumes of the specified container. """
    output = subprocess.check_output([docker_path(), "inspect", container_id])
    result = json.loads(output.decode("utf-8", "ignore"))
    volumes_list = []
    for volume in result[0]["Volumes"]:
        volumes_list.append(volume)
    if rw_only:
        if not ("VolumesRW" in result[0]):
            return []
        for volume in result[0]["Volumes"]:
            if not result[0]["VolumesRW"][volume]:
                volumes_list.remove(volume)
    return volumes_list

def fix_container_name(service_path, service_name, container_name):
    """ Sometimes docker-compose gives us just a shortened name for a
        container. While I am not fully aware of the algorithm, I assume it
        will usually still be unique. In this function, we try to get back to
        the full unshortened name.
    """
    names = get_service_container_names(service_path, service_name)
    if container_name in names:
        return container_name
    matched_name = None
    for name in names:
        if name.startswith(container_name):
            if matched_name != None:
                raise RuntimeError("encountered unexpected non-unique " +\
                    "container label")
            matched_name = name
    return matched_name

def get_running_service_containers(service_path, service_name):
    """ Get all running containers of the given service.
        Returns a list of container ids.
    """
    running_containers = []
    try:
        output = subprocess.check_output([docker_compose_path(), "ps"],
            cwd=service_path, stderr=subprocess.STDOUT,
            timeout=10).\
            decode("utf-8", "ignore")
    except subprocess.CalledProcessError as e:
        output = e.output.decode("utf-8", "ignore")
        if output.find("client and server don't have same version") >= 0:
            print_msg("error: it appears docker-compose is installed with " +\
                "a version incompatible to docker.", color="red")
            sys.exit(1)
        raise e
    output = output.\
        replace("\r", "\n").replace("\n\n", "").split("\n")

    skipped_past_dashes = False
    for output_line in output:
        if len(output_line.strip()) == 0:
            continue
        if output_line.startswith("---------"):
            skipped_past_dashes = True
            continue
        output_line = output_line.strip()
        if len(output_line) > 0 and output_line.find(" Up") > 0:
            # this is a running container!
            space_pos = output_line.find(" ")
            running_containers.append(output_line[:space_pos])
    return [fix_container_name(service_path, service_name, container) \
        for container in running_containers]

def get_permission_info_from_yml(service_path, service_name):
    """ Get permission info for the given service
    """
    f = None
    try:
        f = open(os.path.join(service_path, "permissions.yml"), "rb")
    except Exception as e:
        return {"livedata-permissions" : {}}
    perm_dict = dict()
    current_area = None
    contents = f.read().decode("utf-8", "ignore").replace("\r\n", "\n").\
        replace("\r", "\n").replace("\t", " ").split("\n")
    for line in contents:
        if line.strip() == "":
            continue
        if not line.startswith(" ") and not line.startswith("\t"):
            line = line.strip()
            if not line.endswith(":"):
                print_msg("error: syntax error in permissions.yml: " +\
                    "colon expected", service=service, color="red")
                raise RuntimeError("invalid syntax")
            line = line[:-1]
            # this specifies the current area
            if line == "livedata-permissions":
                current_area = "livedata-permissions"
            else:
                print_msg("warning: unrecognized permissions.yml " +\
                    "section: " + str(line), service=service,
                    color="red")
                continue
            if not current_area in perm_dict:
                perm_dict[current_area] = dict()
            continue
        elif line.startswith(" ") or line.startswith("\t"):
            if current_area == None:
                print_msg("error: syntax error in permissions.yml: " +\
                    "unexpected value outside of block",
                    service=service, color="red")
                raise RuntimeError("invalid syntax")
            k = line.partition(":")[0].strip()
            v = line.partition(":")[2].strip()
            perm_dict[current_area][k] = v

            continue
    f.close()
    
    # make sure some stuff is present:
    if not "livedata-permissions" in perm_dict:
        perm_dict["livedata-permissions"] = dict()

    return perm_dict


def get_volumes_from_compose_yml(service_path, service_name, rw_only=False):
    """ Attempt to parse and return all volumes used by a service from the
        according docker-compose.yml of that service.
    """
    volumes = []
    f = open(os.path.join(service_path, "docker-compose.yml"), "rb")
    contents = f.read().decode("utf-8", "ignore").replace("\r\n", "\n").\
        replace("\r", "\n").replace("\t", " ").split("\n")
    in_volume_list = False
    for line in contents:
        if line.strip() == "":
            continue
        if line.startswith(" ") and (line.strip().startswith("volumes ")
                or line.strip().startswith("volumes:")):
            in_volume_list = True

            # parse remaining stuff in line:
            i = line.find("volumes")
            line = line[i+len("volumes"):].strip()
            if line.startswith(":"):
                line = line[1:].strip()
            if len(line) == 0:
                continue

        if in_volume_list:
            if not line.strip().startswith("-"):
                in_volume_list = False
                continue
            line = line[line.find("-")+1:].strip()
            parts = line.split(":")
            
            # check if read-only or not:
            rwro = "ro"
            if len(parts) >= 3:
                if parts[2] == "rw":
                    rwro = "rw"

            # make sure path is absolute:
            if len(parts) >= 2:
                if not os.path.isabs(parts[0]):
                    parts[0] = os.path.join(service_path, parts[0])
                    parts[0] = os.path.normpath(os.path.abspath(parts[0]))

            # add to list:
            if len(parts) >= 2 and (rwro == "rw" or not rw_only):
                volumes.append(parts[0])
    f.close()
    return volumes

def get_service_volumes(service_path, service_name, rw_only=False):
    """ This determines to get all volumes a service uses from as many sources
        as possible (currently from docker-compose.yml and from inspecting the
        associated running containers if any).
        Returns a list of absolute paths to the according volumes.
    """
    resulting_volumes = set()
    for container in get_running_service_containers(service_path,
            service_name):
        volumes = get_existing_container_volumes(container, rw_only=rw_only)
        for volume in volumes:
            volume = os.path.join(service_path, volume)
            volume = os.path.normpath(os.path.abspath(volume))
            resulting_volumes.add(volume)
    for volume in get_volumes_from_compose_yml(service_path, service_name,
            rw_only=rw_only):
        resulting_volumes.add(volume)
    return resulting_volumes

def get_volume_mount(path):
    if not os.path.exists(path):
        raise ValueError("given path does not exist: " + str(path))
    output = subprocess.check_output([locate_binary("df"), path]).\
        decode("utf-8", "ignore").strip()

    # skip first line:
    if output.find("\n") <= 0:
        raise RuntimeError("failed to parse df output")
    output = output[output.find("\n")+1:]

    # skip past first entry:
    skip_pos = output.find(" ")
    if skip_pos <= 0 or skip_pos >= len(output):
        raise RuntimeError("failed to parse df output")
    output = output[skip_pos+1:].strip()

    # skip past all entries not starting with /
    while True:
        fwslash = output.find("/")
        spacepos = output.find(" ")
        if fwslash < 0:
            raise RuntimeError("failed to parse df output")
        if spacepos >= 0 and spacepos < fwslash:
            output = output[spacepos+1:].strip()
            continue
        break

    # the remaining entry should be the mount point:
    if not os.path.exists(output):
        raise RuntimeError("returned mountpoint expected " +\
            "to be directory, but apparently it doesn't exist")

    return output

def subvolume_stat_check(path):
    output = subprocess.check_output([locate_binary("stat"),
        "-c", "%i", path]).decode('utf-8', 'ignore').strip()
    return (output == "256")

def btrfs_is_subvolume(path):
    """ Check if the given path is a btrfs subvolume.
    """
    nontrivial_error = "error: failed to map a btrs subvolume "+\
        "to its POSIX path. This seems to be a non-trivial setup."+\
        " You should probably do your snapshotting manually!!"

    # first, get the containing mount point:
    mount = get_volume_mount(os.path.normpath(path + "/../"))

    # get btrfs subvolume list:
    output = subprocess.check_output([locate_binary("btrfs"),
        "subvolume", "list", path]).\
        decode("utf-8", "ignore").strip().split("\n")
    for line in output:
        if not line.startswith("ID ") or line.find(" path ") < 0:
            raise RuntimeError("unexpected btrfs tool output - " + \
                "maybe incompatible tool version? Please report this." +\
                " Full output: " + str(output))
        line = line[line.find(" path ")+len(" path "):].strip()
        full_path_guess = mount + line
        if not os.path.exists(full_path_guess):
            if line == "DELETED":
                continue
            print_msg(nontrivial_error, color="red")
            sys.exit(1)
        if os.path.normpath(os.path.abspath(full_path_guess)) == \
                os.path.normpath(os.path.abspath(path)):
            try:
                output = subprocess.check_output([locate_binary("stat"),
                    "-c", "%i", path]).decode('utf-8', 'ignore').strip()
            except subprocess.CalledProcessError as e:
                # stat failed, although btrfs subvolume list lists it!
                print_msg(nontrivial_error, color="red")
                sys.exit(1)
            if output != "256":
                # not a subvolume, although btrfs subvolume list lists it!
                print_msg(nontrivial_error, color="red")
                sys.exit(1)
            return True
    try:
        output = subprocess.check_output([locate_binary("stat"),
            "-c", "%i", path]).decode('utf-8', 'ignore').strip()
    except subprocess.CalledProcessError as e:
        pass
    if output == "256":
        # stat says it's a subvolume, although we don't think it is!
        print_msg(nontrivial_error, color="red")
        sys.exit(1)
    return False

def check_running_snapshot_transaction(service_path, service_name):
    if os.path.exists(os.path.join(
            service_path, ".docker-services-snapshot.lock")):
        output = subprocess.check_output(
            "ps aux | grep docker-services | grep -v grep | wc -l",
            shell=True).decode("utf-8", "ignore")
        if output.strip() != "1":
            # another copy still running??
            return True
        print_msg("warning: stale snapshot lock found but no process " +\
            "appears to be left around, removing.",
            color="yellow", service=service_name)
        # no longer running, remove file:
        os.remove(os.path.join(service_path,
            ".docker-services-snapshot.lock"))
    return False

def clean_up():
    """ This function will check the status of all docker containers, and then
        irrevocably delete all containers that aren't running.
    """
    print_msg("cleaning up stopped containers...")
    output = subprocess.check_output([docker_path(), "ps", "-a"])
    output = output.decode("utf-8", "ignore").\
        replace("\r", "\n").\
        replace("\n ", "\n").replace(" \n", "\n").replace("\n\n", "\n")
    while output.find("   ") >= 0:
        output = output.replace("   ", "  ")
    output = output.replace("  ", "\t")
    output = output.replace("\t ", "\t").replace(" \t", "\t")
    output = output.split("\n")
    for output_line in output:
        if len(output_line.strip()) == 0:
            continue
        parts = output_line.split("\t")
        if parts[0] == "CONTAINER ID":
            continue
        if len(parts) < 5 or (not parts[3].endswith("ago")):
            print_msg("WARNING: skipping container " + parts[0] +\
                ", cannot locate STATUS column")
            continue
        if parts[0].find(" ") >= 0:
            print_msg("WARNING: skipping container with invalid " +\
                "container id: " + parts[0])
            continue
        if len(parts) == 6 and parts[4].find("_") >= 0:
            parts = parts[:4] + [ '' ] + parts[4:]
        if parts[4] == "" or parts[4].startswith("Exited "):
            print_msg("deleting stopped container " + parts[0] + "...")
            subprocess.check_output([docker_path(), "rm", parts[0]])
    print_msg("cleaning up unneeded images...")
    subprocess.check_output([docker_path(), "rmi",
        "$(docker images -aq"], shell=True)
    print_msg("cleaning up dangling volumes...")
    dangling_vols = subprocess.check_output([docker_path(),
        "volume", "ls", "-qf", "dangling=true"])
    for vol in dangling_vols.splitlines():
        vol = vol.strip()
        if len(vol) == 0:
            continue
        subprocess.check_output([docker_path(), "volume", "rm", vol])

parser = argparse.ArgumentParser(description=textwrap.dedent('''\
    Docker Services: a launch helper when you got more than one
    docker-compose.yml-powered container cloud that you need to power up.'''
    ),
    formatter_class=DoubleLineBreakFormatter)
parser.add_argument("action",
    help=textwrap.dedent('''\
    Possible values:
    
    "list": list all known services.

    "start": start the service specified as argument (or "all" for all).

    "stop": stop the service specified as argument (or "all" for all).

    "restart": restart the service specified as argument (or "all" for all).

    "logs": output the logs of all the docker containers of the service
            specified as argument (or "all" for all).

    "shell": start an interactive shell in the specified service's specified
             subservice (parameters: <service> <subservice>) - the subservice
             is optional if the docker-compose.yml has just one
             container/subservice.

    "snapshot": store an atomic snapshot of the live data of the service
                specified as argument (from livedata/) in livedata-snapshots/

    "clean": clean up all stopped containers. THIS IS NOT REVERSIBLE. The
             docker images of course won't be touched. ''')
    )
parser.add_argument("argument", nargs="*", help="argument(s) to given action")
if len(" ".join(sys.argv[1:]).strip()) == 0:
    parser.print_help()
    sys.exit(1)
args = parser.parse_args()

ensure_docker = docker_path()
ensure_docker_compose = docker_compose_path()

def verify_service_names(names):
    """ This will evaluate all service names passed as command line arguments,
        and make sure they actually exist (and find out their path). "all" is
        interpreted and turned into a list of all services. The resulting
        validated list is returned.
    """
    specified_services = []
    for specified_service in names:
        if specified_service == "all":
            return list(services)
        found = None
        for service in services:
            if service['name'] == specified_service:
                found = service
                break
        if found == None:
            print("docker-services.py: error: no such service found: " +\
                specified_service, file=sys.stderr)
            sys.exit(1)
        specified_services.append(found)
    return specified_services

def is_service_running(path, name):
    """ Check if at least 1 container of this service is currently
        instantiated and active/running.
    """
    if len(get_running_service_containers(path, name)) > 0:
        return True
    return False

def btrfs_tool_check():
    # make sure the btrfs tool is working:
    btrfs_path = locate_binary("btrfs")
    if btrfs_path == None:
        print_msg("error: btrfs tool not found. Are btrfs-progs installed?",
            color="red")
        sys.exit(1)
    output = None
    try:
        output = subprocess.check_output([btrfs_path, "--version"],
            stderr=subprocess.STDOUT).decode("utf-8", "ignore")
    except subprocess.CalledProcessError as e:
        output = e.output.decode("utf-8", "ignore")
    if not output.lower().startswith("btrfs-progrs ") and \
            not output.lower().startswith("btrfs-progs ") and \
            not output.lower().startswith("btrfs "):
        print_msg("error: btrfs tool returned unexpected string. Are " +\
            "btrfs-progrs installed and working?",
            color="red")
        print("Full btrfs output: " + str(output))
        sys.exit(1)

def snapshot_subvolume_readiness_check(service_path, service_name):
    """ Check if the given service is ready for snapshotting or still needs
        btrfs subvolume conversion. Print a warning if not.
    """
    fs = filesystem_of_path(service["folder"])
    if fs != "btrfs":
        return

    btrfs_tool_check()

    if os.path.exists(os.path.join(service_path, "livedata")):
        if not btrfs_is_subvolume(os.path.join(service_path, "livedata"))\
                and len(get_service_volumes(service_path, service_name,\
                rw_only=True)) > 0:
            if is_service_running(service_path, service_name):
                print_msg("the livedata/ dir of this service will " +\
                    "still need to be converted to a subvolume to " +\
                    "enable snapshots.\n" + \
                    "Fix it by doing this:\n" + \
                    "1. Stop the service with: docker-services.py stop " +\
                        service_name + "\n" + \
                    "2. Snapshot the service once with: docker-services.py "+\
                        "snapshot " + service_name + "\n",
                    service=service_name, color="yellow")
                return
            else:
                print_msg("the livedata/ dir of this service still " +\
                    "needs conversion to btrfs subvolume.\n" +\
                    "Fix it by snapshotting it once with: " +\
                    "docker-services.py "+\
                    "snapshot " + service_name + "\n",
                    service=service_name, color="yellow")
                return
    # everything seems fine so far.
    return

def snapshot(directory, service):
    """ Make a backup of the live data of this service. """

    btrfs_tool_check()
    btrfs_path = locate_binary("btrfs")

    # make sure no snapshot is already in progress:
    if check_running_snapshot_transaction(directory, service):
        print_msg("error: snapshot already in progress. " +\
            "try again later", service=service,
            color="red")
        print_msg("remove .docker-services-snapshot.lock if that is " +\
            "incorrect", service=service)
        return False

    print_msg("considering for snapshot...", service=service, color="blue")

    # check which volumes this service has:
    volumes = get_service_volumes(directory, service, rw_only=True)
    if len(volumes) == 0:
        print_msg("service has no read-write volumes, nothing to do.",
            service=service, color="blue")
        return True
    
    # check if we have livedata/:
    if not os.path.exists(os.path.join(directory, "livedata")):
        print_msg("error: service has read-write volumes, " + \
            "but no livedata/ " +\
            "folder. fix this to enable snapshots", service=service,
            color="red")
        return False

    # check if we have any volumes which are actually in livedata/:
    empty_snapshot = True
    for volume in volumes:
        relpath = os.path.relpath(
            os.path.realpath(volume),
            os.path.realpath(os.path.join(directory, "livedata")),
        )
        if relpath.startswith(os.pardir + os.sep):
            # volume is not in livedata/!
            print_msg("warning: volume " + str(volume) + \
                " is NOT in livedata/ - " +\
                "not covered by snapshot!", service=service, color="yellow")
        else:
            empty_snapshot = False
    if empty_snapshot:
        print_msg("this snapshot would be empty because no read-write " +\
            "volumes are mounted to livedata/ - skipping.",
            service=service, color="blue")
        return True

    # check if filesystem of livedata/ is actually btrfs:
    fs = filesystem_of_path(os.path.join(directory, "livedata"))
    if fs != "btrfs":
        print_msg("error: livedata/ has other filesystem " + str(fs) + \
            ", should be btrfs!")
        return fs

    livedata_renamed_dir = os.path.join(directory, ".livedata-predeletion-renamed")
    livedata_dir = os.path.join(directory, "livedata")
    snapshot_dir = os.path.join(directory, ".btrfs-livedata-snapshot")
    tempvolume_dir = os.path.join(directory, ".btrfs-livedata-temporary-volume")
    tempdata_dir = os.path.join(directory, ".livedata-temporary-prevolume-copy")

    # make sure the livedata/ dir is a btrfs subvolume:    
    if is_service_running(directory, service):
        if not btrfs_is_subvolume(livedata_dir):
            print_msg("error: can't do btrfs subvolume conversion because "+\
                "service is running. The first snapshot is required to " +\
                "be done when the service is stopped.", service=service,
                color="red")
            return False

    lock_path = os.path.join(directory, ".docker-services-snapshot.lock")

    # add a transaction lock:
    transaction_id = str(uuid.uuid4())
    with open(lock_path, "wb") as f:
        f.write(transaction_id.encode("utf-8"))
    
    # wait a short amount of time so other race condition writes will
    # be finished with a very high chance:
    time.sleep(0.5)

    # verify we got the transaction lock:
    contents = None
    with open(lock_path, "rb") as f:
        contents = f.read().decode("utf-8", "ignore")
    if contents.strip() != transaction_id:
        print_msg("error: mid-air snapshot collision detected!! " + \
            "Did you call the script twice?", service=service, color="red")
        return False

    # make sure the .livedata-predeletion-renamed isn't there:
    if os.path.exists(livedata_renamed_dir):
        if not os.path.exists(livedata_dir):
            print_msg("warning: .livedata-predeletion-renamed/ " + \
                "is present and no livedata/ folder." +\
                "Moving it back...",
                service=service, color="yellow")
            shutil.move(livedata_renamed_dir, livedata_dir)
            assert(not os.path.exists(livedata_renamed_dir))
        else:
            print_msg("error: .livedata-predeletion-renamed/ " + \
                "is still there, indicating a previously aborted " +\
                "run, but livedata/ is also still around. " +\
                "Please figure out which one you want to keep, and " +\
                "delete one of the two.", service=service,
                color="red")
            sys.exit(1)

    # make sure the .livedata-temporary-prevolume-copy directory is unused:
    if os.path.exists(tempdata_dir):
        print_msg("warning: .livedata-temporary-prevolume-copy/ " + \
            "already present! " +\
            "This is probably a leftover from a previously " + \
            "aborted attempt. Will now attempt to delete it...",
            service=service, color="yellow")
        shutil.rmtree(tempdata_dir)
        assert(not os.path.exists(tempdata_dir))

    # make sure the btrfs snapshot path is unused:
    if os.path.exists(snapshot_dir):
        if subvolume_stat_check(snapshot_dir):
            print_msg("warning: .btrfs-livedata-snapshot/ already present! " \
                + "This is probably a leftover from a previously " + \
                "aborted attempt. Will now attempt to delete it...",
                service=service, color="yellow")
            subprocess.check_output([btrfs_path, "subvolume",
                "delete", snapshot_dir])
            assert(not os.path.exists(snapshot_dir))
        else:
            print_msg("error: .btrfs-livedata-snapshot/ already present, " \
                + "but it is not a btrfs snapshot!! I don't know how " + \
                "to deal with this, aborting.", service=service,
                color="red")
            return False

    # make sure the temporary btrfs subvolume path is unused:
    if os.path.exists(tempvolume_dir):
        print_msg("warning: .btrfs-livedata-temporary-volume/ already " +\
            "present! " \
            + "This is probably a leftover from a previously " + \
            "aborted attempt. Will now attempt to delete it...",
            service=service, color="yellow")
        output = subprocess.check_output([btrfs_path, "subvolume",
            "delete", tempvolume_dir])
        assert(not os.path.exists(tempvolume_dir))

    # if this isn't a btrfs subvolume, we will need to fix that first:
    if not btrfs_is_subvolume(livedata_dir):
        print_msg("warning: initial subvolume conversion required. "+\
            "DON'T TOUCH livedata/ WHILE THIS HAPPENS!!",
            service=service, color="yellow")
        try:
            output = subprocess.check_output([btrfs_path, "subvolume",
                "create", tempvolume_dir])
        except Exception as e:
            os.remove(lock_path)
            raise e
        assert(btrfs_is_subvolume(tempvolume_dir))

        # copy all contents:
        assert(not os.path.exists(tempdata_dir))
        shutil.copytree(livedata_dir, tempdata_dir, symlinks=True)
        assert(os.path.exists(tempdata_dir))
        for f in os.listdir(tempdata_dir):
            orig_path = os.path.join(tempdata_dir, f)
            new_path = os.path.join(tempvolume_dir, f)
            shutil.move(orig_path, new_path)

        # do a superficial check if we copied all things:
        copy_failed = False
        for f in os.listdir(tempvolume_dir):
            if not os.path.exists(os.path.join(livedata_dir, f)):
                copy_failed = True
                break
        for f in os.listdir(livedata_dir):
            if not os.path.exists(os.path.join(tempvolume_dir, f)):
                copy_failed = True
                break
        if copy_failed:
            print_msg("error: files of old livedata/ directory and "+\
                "new subvolume do not match. Did things get changed "+\
                "during the process??", service=service, color="red")
            return False

        # remove old livedata/ dir:
        propagate_interrupt = None
        while True:
            try:
                shutil.move(livedata_dir, livedata_renamed_dir)
                shutil.move(tempvolume_dir, livedata_dir)
                shutil.rmtree(livedata_renamed_dir)
                break
            except KeyboardInterrupt as e:
                propagate_interrupt = e
                continue
        if propagate_interrupt != None:
            raise propagate_interrupt
        print_msg("conversion of livedata/ to btrfs subvolume complete.",
            service=service)

    snapshots_dir = os.path.join(directory, "livedata-snapshots")

    # create livedata-snapshots/ if not present:
    if not os.path.exists(snapshots_dir):
        os.mkdir(snapshots_dir)

    # go ahead and snapshot:
    print_msg("initiating btrfs snapshot...",
        service=service)
    output = subprocess.check_output([btrfs_path, "subvolume", "snapshot",
        "-r", "--", livedata_dir, snapshot_dir])
    
    # copy snapshot to directory:
    now = datetime.datetime.now()
    snapshot_base_name = str(now.year)
    if now.month < 10:
        snapshot_base_name += "0"
    snapshot_base_name += str(now.month)
    if now.day < 10:
        snapshot_base_name += "0"
    snapshot_base_name += str(now.day)
    snapshot_base_name += "-"
    if now.hour < 10:
        snapshot_base_name += "0"
    snapshot_base_name += str(now.hour)
    if now.minute < 10:
        snapshot_base_name += "0"
    snapshot_base_name += str(now.minute)
    if now.second < 10:
        snapshot_base_name += "0"
    snapshot_base_name += str(now.second)
    snapshot_name = snapshot_base_name + "00"
    i = 1
    while os.path.exists(os.path.join(snapshots_dir, snapshot_name)):
        snapshot_name = snapshot_base_name
        if i < 10:
            snapshot_name += "0"
        snapshot_name += str(i)
        i += 1
    snapshot_specific_dir = os.path.join(snapshots_dir,
        snapshot_name)
    print_msg("copying to " + snapshot_specific_dir,
        service=service)
    shutil.copytree(snapshot_dir, snapshot_specific_dir, symlinks=True)
    subprocess.check_output([btrfs_path, "subvolume",
        "delete", snapshot_dir])
    assert(not os.path.exists(snapshot_dir))
    print_msg("snapshot complete.", service=service, color="green")
    
def unknown_action(hint=None):
    """ Print an error that the given action to docker-services.py is invalid,
        with a possible hint to suggest another action.
    """
    print("docker-services.py: error: unknown action: " + \
        args.action, file=sys.stderr)
    if hint != None:
        print("Did you mean: " + str(hint) + "?")
    sys.exit(1)

# scan for services:
def scan_dir(d):
    for f in os.listdir(d):
        if os.path.isdir(os.path.join(d, f)):
            if os.path.exists(os.path.join((os.path.join(d, f)),
                    "docker-compose.yml")):
                def is_service_present(path):
                    """ Check if we already have listed such a service in our
                        collected list.
                    """
                    for service in services:
                        if os.path.normpath(os.path.abspath(service["folder"]\
                                )) == os.path.normpath(os.path.abspath(path)):
                            return True
                    return False
                if not is_service_present(os.path.join(d, f)):
                    services.append({
                        'folder' : os.path.normpath(os.path.abspath(\
                            os.path.join(d, f))),
                        'name' : f,
                    })
scan_dir(os.getcwd())
if os.path.exists(os.path.join(os.path.expanduser("~"), ".docker-services")):
    scan_dir(os.path.join(os.path.expanduser("~"), ".docker-services"))
if os.path.exists("/usr/share/docker-services"):
    scan_dir("/usr/share/docker-services")

# Ensure the docker main service is running:
error_output = None
try:
    subprocess.check_output([docker_path(), "ps"],
        stderr=subprocess.STDOUT)
except subprocess.CalledProcessError as e:
    error_output = e.output.decode("utf-8", "ignore")
if error_output != None:
    if error_output.find("dial unix") >= 0 and \
            error_output.find("no such file or directory") >= 0:
        print("docker-services.py: error: " + \
            "docker daemon appears to be not running." +\
            " Please start it.")
        sys.exit(1)
    else:
        print("docker-services.py: error: " + \
            "there appears to be some unknown problem with " + \
            "docker! (test run of \"docker ps\" returned error code)")
        sys.exit(1)

# check if services are btrfs ready:
if args.action != "snapshot":
    for service in services:
        snapshot_subvolume_readiness_check(service["folder"], service["name"])

# --- Main handling of actions here:

if args.action == "list" or args.action == "ps" or args.action == "status":
    print("Service list (" + str(len(services)) + " service(s)):")
    for service in services:
        state = ""
        if is_service_running(service["folder"], service["name"]):
            state = "\033[1;32mrunning\033[0m"
        else:
            state = "\033[1;31mstopped\033[0m"
        print("\033[0m\033[1m" + service['name'] + "\033[0m, in: " + \
            service['folder'] + ", state: " + state)
elif args.action == "help":
    parser.print_help()
    sys.exit(1)
elif args.action == "logs":
    if len(args.argument) == 0:
        print("docker-services.py: error: please specify the name " + \
            "of the service for which docker logs shold be printed, "+\
            "or \"all\"", file=sys.stderr)
        sys.exit(1)
    specified_services = verify_service_names(args.argument)
    i = 0
    while i < len(specified_services):
        service = specified_services[i]
        names = get_service_container_names(service["folder"],\
            service["name"])
        for name in names:
            print_msg("printing log of container " + name,
                service=service['name'], color='blue')
            try:
                subprocess.check_output([docker_path(), "ps"],
                    stderr=subprocess.STDOUT) 
            except subprocess.CalledProcessError:
                print_msg("failed. maybe container has no logs yet?",
                    service=service['name'], color='yellow')
                pass
        i += 1
elif args.action == "shell":
    if len(args.argument) == 0:
        print("docker-services.py: error: please specify the name " + \
            "of the service for which an interactive shell should be started",
            file=sys.stderr)
        sys.exit(1)
    if len(args.argument) > 1:
        specified_services = verify_service_names(args.argument[:-1])
    else:
        specified_services = verify_service_names(args.argument)
    if len(specified_services) != 1:
        print("docker-services.py: error: this command can only be used " + \
            "on a single service",
            file=sys.stderr)
        sys.exit(1)
    service = specified_services[0]
    names = get_service_container_names(service["folder"],\
        service["name"])
    if len(names) == 0:
        print("docker-services.py: error: the specified service " +\
            "doesn't seem to have any subservices/containers",
            file=sys.stderr)
        sys.exit(1)
    if len(names) > 1:
        if len(args.argument) == 1:
            print("docker-services.py: error: this service has " +\
                str(names) + " subservices/containers. Therefore, "+\
                "specify the name of the subservice.",
                file=sys.stderr)
            sys.exit(1)
        for name in names:
            if name.startswith(args.argument[-1] + "_"):
                names = [name]
                break
        if len(names) > 1:
            print("docker-services.py: error: specified subservice " +\
                "not found", 
                file=sys.stderr)
            sys.exit(1)
    if len(args.argument) > 1 and (not names[0].startswith(\
            args.argument[-1])):
        print("docker-services.py: error: specified subservice " +\
            "not found",
            file=sys.stderr)
        sys.exit(1)
    if names[0] in get_running_service_containers(service["folder"], \
            service["name"]):
        print_msg("attaching to running container " + str(names[0]),
            service=service['name'],\
            color="blue")
        subprocess.call([docker_path(), "exec", "-t", "-i",
                names[0], "/bin/bash"],
            stderr=subprocess.STDOUT)
    else:
        print_msg("launching container " + str(names[0]) + \
            " with shell",
            service=service['name'],\
            color="blue")
        image_name = names[0]
        if image_name.endswith("_1"):
            image_name = image_name[:-len("_1")]
        subservice_name = image_name.partition("_")[2]
        subprocess.call([docker_compose_path(), "run",
            subservice_name, "/bin/bash"], cwd=service["folder"])
elif args.action == "start" or args.action == "restart":
    if len(args.argument) == 0:
        print("docker-services.py: error: please specify the name " + \
            "of the service to be started, or \"all\"", file=sys.stderr)
        sys.exit(1)
    specified_services = verify_service_names(args.argument)
    start_only_services = []

    # collect the required dependencies to launch services:
    dependencies_added = True
    visited_deps = []
    while dependencies_added:
        dependencies_added = False
        deps = []
        for service in specified_services:
            # collect deps:
            new_deps = get_service_dependencies(service["folder"],\
                service["name"])
            new_deps = [new_dep for new_dep in new_deps \
                if new_dep not in visited_deps]
            deps += new_deps
            visited_deps += new_deps
        # see if all dependencies are in our list, if not then add to separate
        # start-only list:
        for dep in deps:
            if not (dep in specified_services) and \
                    not (dep in start_only_services):
                start_only_services = [dep] + start_only_services
                dependencies_added = True

    all_services = start_only_services + specified_services
    i = 0
    while i < len(all_services):
        service = all_services[i]
        if is_service_running(service['folder'], service['name']):
            # restart if requested:
            if args.action == "start" or (service in start_only_services):
                launch_completed += [{"folder":service["folder"],
                    "name":service["name"]}]
                print_msg("already running.", service=service['name'],\
                    color="green")
                i += 1 
                continue
            print_msg("stopping...", service=service['name'],
                color="blue")
            LaunchThreaded.stop(service["folder"], service['name']) 
        if i < len(specified_services) - 1:
            # not the last service, allow it to go to background:
            LaunchThreaded.attempt_launch(service['folder'], service['name'])
        else:
            # last service, handle this one blocking:
            LaunchThreaded.attempt_launch(service['folder'], service['name'],\
                to_background_timeout=None)
        i += 1
    LaunchThreaded.wait_for_launches()
elif args.action == "stop":
    if len(args.argument) == 0:
        print("docker-services.py: error: please specify the name " + \
            "of the service to be stopped, or \"all\"", file=sys.stderr)
        sys.exit(1)
    specified_services = verify_service_names(args.argument)
    i = 0
    while i < len(specified_services):
        service = specified_services[i]
        if is_service_running(service['folder'], service['name']):
            print_msg("stopping...", service=service['name'],
                color="blue")
            LaunchThreaded.stop(service["folder"], service['name'])
            print_msg("stopped.", service=service['name'], color="green")
            i += 1
            continue
        print_msg("not currently running.", service=service['name'],
            color="blue")
        i += 1
elif args.action == "snapshot":
    if len(args.argument) == 0:
        print("docker-services.py: error: please specify the name " + \
            "of the service to be stopped, or \"all\"", file=sys.stderr)
        sys.exit(1)
    specified_services = verify_service_names(args.argument)
    return_error = False
    for service in specified_services:
        fs = filesystem_of_path(service["folder"])
        if fs != "btrfs":
            print_msg("cannot snapshot service. filesystem " +\
                "is " + fs + ", would need to be btrfs",
                service=service["name"], color="red")
            return_error = True
            continue
        if not snapshot(service["folder"], service["name"]):
            return_error = True
    if return_error:
        sys.exit(1)
    else:
        sys.exit(0)
elif args.action == "clean":
    clean_up()
else:
    unknown_action()
