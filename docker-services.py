#!/usr/bin/env python3

"""
Copyright (c) 2015-2016  Jonas Thiem

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

    The snapshots will be atomic, therefore they should be suitable even for
    database realtime operations while the database is running and writing to
    the volume(s).

    ## Enable snapshots for a specific service

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
import copy
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
import traceback
import uuid
yaml_available = False
try:
    import yaml
except ImportError:
    print("IMPORTANT WARNING: using YAML fallback hack. This will work for "+\
        "most generic configs, but lead to errors for advanced YAML files. "+\
        "Please install pyyaml to use proper parsing.", file=sys.stderr)
    pass

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

class SystemInfo(object):
    """ This class provides system info of various sorts about the installed
        docker versions, btrfs and more.
    """
    __cached_docker_path = None

    @staticmethod
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

    @staticmethod
    def docker_path():
        """ Locate docker binary and return its path, or exit process with error
            if not available.
        """
        if SystemInfo.__cached_docker_path != None:
            return SystemInfo.__cached_docker_path
        
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
            bin_path = SystemInfo.locate_binary(test_name)
            if bin_path == None:
                continue
            if behaves_like_docker(bin_path):
                return bin_path
        print("docker-services.py: error: no docker found. Is it installed?")
        sys.exit(1)
    
    @staticmethod
    def is_btrfs_subvolume(path):
        """ Check if the given path is a btrfs subvolume.
        """
        nontrivial_error = "error: failed to map a btrs subvolume "+\
            "to its POSIX path. This seems to be a non-trivial setup."+\
            " You should probably do your snapshotting manually!!"

        # first, get the containing mount point:
        mount = SystemVolume.get_fs_mount_root(os.path.normpath(path + "/../"))

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


    @staticmethod
    def docker_compose_path():
        """ Locate docker-compose binary and return its path, or exit process with
            error if not available.
        """
        bin_path = SystemInfo.locate_binary("docker-compose")
        if bin_path != None:
            return bin_path
        print("docker-services.py: error: no docker-compose found. " + \
            "Is it installed?")
        sys.exit(1)

    @staticmethod
    def btrfs_path():
        """ Locate btrfs helper tool binary and return its path, or return None if
            not found.
        """
        bin_path = locate_binary("btrfs")
        if bin_path != None:
            return bin_path
        return None

    @staticmethod
    def get_fs_mount_root(path):
        if not os.path.exists(path):
            raise ValueError("given path does not exist: " + str(path))
        output = subprocess.check_output([locate_binary("df"), path]).\
            decode("utf-8", "ignore").strip()

        # Skip first line:
        if output.find("\n") <= 0:
            raise RuntimeError("failed to parse df output")
        output = output[output.find("\n")+1:]

        # Skip past first entry:
        skip_pos = output.find(" ")
        if skip_pos <= 0 or skip_pos >= len(output):
            raise RuntimeError("failed to parse df output")
        output = output[skip_pos+1:].strip()

        # Skip past all entries not starting with /
        while True:
            fwslash = output.find("/")
            spacepos = output.find(" ")
            if fwslash < 0:
                raise RuntimeError("failed to parse df output")
            if spacepos >= 0 and spacepos < fwslash:
                output = output[spacepos+1:].strip()
                continue
            break

    @staticmethod
    def filesystem_type_at_path(path):
        """ Find out the filesystem a given directory or file is on and return
            the name (e.g. "ext4", "btrfs", ...)
        """
        if not os.path.exists(path):
            raise ValueError("given path does not exist: " + str(path))
        output = subprocess.check_output([
            SystemInfo.locate_binary("df"), path]).\
            decode("utf-8", "ignore")

        # Skip first line:
        if output.find("\n") <= 0:
            raise RuntimeError("failed to parse df output")
        output = output[output.find("\n")+1:]

        # Get first word being the FS of the path:
        end_pos = output.find(" ")
        if end_pos <= 0:
            raise RuntimeError("failed to parse df output")
        device_of_path = output[:end_pos]
        if device_of_path == "-":
            # We can't find out the filesystem of this path.
            # -> try to find out the parent!
            get_parent = os.path.normpath(path + "/../")
            if get_parent == os.path.normpath(path) or path == "/":
                # We are already at the root.
                return None
            return filesystem_type_at_path(os.path.normpath(path + "/../"))

        output = subprocess.check_output([
            SystemInfo.locate_binary("mount")]).\
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

    def _btrfs_subvolume_stat_check(path):
        output = subprocess.check_output([locate_binary("stat"),
            "-c", "%i", path]).decode('utf-8', 'ignore').strip()
        return (output == "256")

def print_msg(text, service=None, container=None, color="blue"):
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
        service_part = "\033[0m" + color_code() + service
        if container != None:
            service_part = service_part + "/" + container

    docker_services_part = ""
    if service == None or len(service) == 0:
        docker_services_part = color_code() + "docker-services.py"

    initial_length = len("[docker-services.py")
    if service != None:
        initial_length = len("[" + service)
        if container != None:
            initial_length += len("/" + container)
    initial_length += len("] ") 

    print("\033[0m\033[1m[\033[0m" + docker_services_part + \
        service_part + "\033[0m\033[1m] \033[0m" + \
        textwrap.fill(text, 79,
            initial_indent=(" " * initial_length),
            subsequent_indent=(" " * initial_length))[
            initial_length:] + \
        "\033[0m")

class ServiceDependency(object):
    """ This class holds the info describing the dependency to another
        service's container.
    """
    def __init__(self, other_service_name, other_service_path,
            other_service_container_name):
        self.service_name = other_service_name
        self.service_path = other_service_path
        self.container_name = other_service_container_name

    @property
    def container(self):
        """ The actual container instance to start/stop the container which
            is the target of this dependency.

            Please note this might be unavailable if the container doesn't
            belong to any known service, in which case accessing this property
            will raise ValueError.
        """
        if self.service_name == None:
            raise ValueError("the service that provides this dependency " +\
                "isn't known")
        return ServiceContainer(self.service_name, self.service_path,
            self.container_name)

    def __repr__(self):
        if self.service_name != None:
            return self.service_name + "/" + self.container_name
        return self.container_name + " (unknown service!!)"

class ServiceContainer(object):
    """ An instance of this class holds the info for a service's container.
        It can be used to e.g. obtain the system-wide docker container name,
        or the directory for the respective docker-compose.yml where
        docker-compose commands can be run.
    """
    def __init__(self, service_name, service_path, container_name,
            image_name=None):
        self.service_name = service_name
        self.service_path = service_path
        self.name = container_name
        self._known_image_name = image_name

    def __repr__(self):
        return self.service_name + "/" + self.name

    def __hash__(self):
        return hash(self.service_name + \
            os.path.normpath(os.path.abspath(self.service_path)) + \
            self.name)

    def __eq__(self, other):
        if other is None:
            return False
        if not hasattr(other, "service_name") or not hasattr(other,
                "service_path") or not hasattr(other, "name"):
            return False
        if other.service_name != self.service_name:
            return False
        if (os.path.normpath(os.path.abspath(self.service_path)) !=
                os.path.normpath(os.path.abspath(other.service_path))):
            return False
        if other.name != self.name:
            return False
        return True

    def __neq__(self, other):
        return not self.__eq__(other)

    def launch(self):
        if self.running:
            return
        subprocess.check_call([SystemInfo.docker_compose_path(),
            "rm", "-f", self.name],
            cwd=self.service.service_path)
        subprocess.check_call([SystemInfo.docker_compose_path(),
            "build", self.name],
            cwd=self.service.service_path)
        subprocess.check_call([SystemInfo.docker_compose_path(),
            "up", "-d", self.name],
            cwd=self.service.service_path)

    @property
    def stop(self):
        subprocess.check_call([SystemInfo.docker_compose_path(),
            "stop", self.name], cwd=self.service.service_path)

    @property
    def running(self):
        names = self.service._get_running_service_container_names()
        return (self.name in names)

    @property
    def service(self):
        """ The service this container belongs to. """
        return Service(self.service_name, self.service_path)

    @property
    def default_container_name(self):
        fpath = os.path.normpath(os.path.abspath(self.service_path))
        return (os.path.basename(fpath).replace("-", "")
            + "_" + str(self.name) + "_1")

    @property
    def image_name(self):
        if self._known_image_name != None:
            return self._known_image_name
        return self.default_container_name

    @property
    def current_running_instance_name(self):
        return self.default_container_name

    def _get_active_volume_directories(self, rw_only=False):
        """ Get the volumes active in this container. If the container hasn't
            been started before, this might raise a ValueError since this
            information is obtained via docker inspection of the container.
        """
        try:
            output = subprocess.check_output([SystemInfo.docker_path(),
                "inspect",
                container_id])
        except subprocess.CalledProcessError:
            raise ValueError("container not created - you might need to "+\
                "launch it first")
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

    def _map_host_dir_to_container_volume_dir(self, volume_dir):
        """ Attempts to find the volume mount information and return the host
            directory currently mapped to the given container volume path.
        """
        try:
            output = subprocess.check_output([SystemInfo.docker_path(),
                "inspect",
                container_id])
        except subprocess.CalledProcessError:
            raise ValueError("container not created - you might need to "+\
                "launch it first")
        result = json.loads(output.decode("utf-8", "ignore"))
        for mount in result[0]["Mounts"]:
            if os.path.normpath(mount["Destination"]) == \
                    os.path.normpath(volume_dir):
                return mount["Source"]
        return None

    @property
    def active_volumes(self):
        """ Get all the active volumes of this container. May return an empty
            or outdated list if the container is stopped.

            Returns a list of tuples (host file system path,
                container filesystem path).
        """

        try:
            return [(volume, self._map_host_dir_to_container_volume_dir(
                volume)) for volume in self._get_active_volumes()]
        except ValueError:
            return []

    @property
    def active_writable_volumes(self):
        """ Get all the active volumes of this container which are mounted
            with write capabilities.
            May return an empty list if the container is stopped.

            Returns a list of tuples (host file system path,
                container filesystem path).
        """
        try:
            return [(volume, self._map_host_dir_to_container_volume_dir(
                volume)) for volume in self._get_active_volumes(rw_only=True)]
        except ValueError:
            return []

    @property
    def config_specified_volumes(self, rw_only=False):
        """ Attempt to parse and return all volumes used by this container
            from the according docker-compose.yml.

            Returns a list of tuples (host file system path,
                container filesystem path).
        """
        volumes = []
        def parse_volume_line(parts):
            # Check if read-only or not:
            rwro = "rw"
            if len(parts) >= 3:
                if parts[2] == "ro":
                    rwro = "ro"

            # Make sure path is absolute:
            if len(parts) >= 2:
                if not os.path.isabs(parts[0]):
                    parts[0] = os.path.join(service_path, parts[0])
                    parts[0] = os.path.normpath(os.path.abspath(parts[0]))

            # Add to list:
            if len(parts) >= 2 and (rwro == "rw" or not rw_only):
                volumes.append((parts[0], parts[1]))

        f = open(os.path.join(self.service_path,
            "docker-compose.yml"), "rb")
        try:
            contents = f.read().decode("utf-8", "ignore").\
                replace("\r\n", "\n").\
                replace("\r", "\n").replace("\t", " ")
        finally:
            f.close()
        try:
            parsed_obj = yaml.safe_load(contents)
            if self.name in parsed_obj and \
                    "volumes" in parsed_obj[self.name]:
                for line in parsed_obj[self.name]["volumes"]:
                    parse_volume_line(parts.split(":")) 
        except NameError:
            pass

        in_relevant_container_section = False
        in_volume_list = False
        for line in contents.split("\n"):
            if line.strip() == "":
                continue

            # Figure out whether we are entering/leaving the relevant container
            # section:
            if line.startswith(self.name + ":"):
                in_relevant_container_section = True
            elif not line.startswith(" ") and \
                    not line.startswith("\t") and \
                    line.endswith(":"):
                in_relevant_container_section = False

            if line.startswith(" ") and (line.strip().startswith("volumes ")
                    or line.strip().startswith("volumes:")):
                in_volume_list = True

                # Parse remaining stuff in line:
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
                if line.startswith("\"") and line.endswith("\""):
                    line = line[1:-1]
                parts = line.split(":")
                parse_volume_line(parts) 
        return volumes

    @property
    def volumes(self):
        """ A list of tuples (host file path, container file path) of all the
            volumes this container possibly has, collected from as many
            sources as possible like the docker-compose.yml and container
            inspection.
        """
        volumes = []
        for vol1 in self.active_volumes:
            volumes.append(vol1)
        for vol2 in self.config_specified_volumes():
            volumes.append(vol2)

    @property
    def rw_only_volumes(self):
        """ A list of tuples (host file path, container file path) of all the
            volumes this container possibly has, collected from as many
            sources as possible like the docker-compose.yml and container
            inspection.
        """
        volumes = []
        for vol1 in self.active_writable_volumes:
            volumes.append(vol1)
        for vol2 in self.config_specified_volumes(rw_only=True):
            volumes.append(vol2)

    @property
    def dependencies(self):
        # Collect all external_links and links container references:
        external_links = self.service._external_docker_compose_links(
            self.name)
        internal_links = self.service._internal_docker_compose_links(
            self.name)
        dependencies = []

        # Go through all external links
        for link in external_links:
            link_name = link.partition(":")[0]
            target_found = False
            # See if we can find the target service:
            for service in Service.all():
                for container in service.containers:
                    if (service == self.service and 
                            link_name == container.name) or \
                            link_name == container.default_container_name:
                        target_found = True
                        dependencies.append(ServiceDependency(
                            service.name, service.service_path,
                            container.name))
            if not target_found:
                dependencies.append(ServiceDependency(
                    None, None, link_name))

        # Go through all internal links:
        for link in internal_links:
            link_name = link.partition(":")[0]
            target_found = False
            # See if we can find the target service:
            for container in self.service.containers:
                 if link_name == container.name:
                    target_found = True
                    dependencies.append(ServiceDependency(
                        self.service_name, self.service_path,
                        container.name))
            if not target_found:
                dependencies.append(ServiceDependency(
                    None, None, link_name))
        return dependencies

class Service(object):
    """ This class holds all the info and helper functionality for managing
        a service, whereas a service is a group of containers with one single
        docker-compose.yml stored in a subfolder in the global service
        directory.
    """
    def __init__(self, service_name, service_path):
        self.name = service_name
        self.service_path = service_path

    def __eq__(self, other):
        if not hasattr(other, "name") and not hasattr(other,
                "service_path"):
            return False
        if self.name == other.name:
            if os.path.normpath(os.path.abspath(self.service_path)) == \
                    os.path.normpath(os.path.abspath(other.service_path)):
                return True
        return False

    def __neq__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.name + "/" + os.path.normpath(
            os.path.abspath(self.service_path)))

    def is_running(self):
        return len(self._get_running_service_container_names()) > 0

    @staticmethod
    def find_by_name(name):
        for service in Service.all():
            if service.name == name:
                return service
        return None

    @staticmethod
    def all():
        """ Get a global list of all detected services.
        """
        services = []
        def scan_dir(d):
            if not os.path.isdir(d):
                return
            d = os.path.abspath(d)
            for f in os.listdir(d):
                if not os.path.isdir(os.path.join(d, f)):
                    continue
                if not os.path.exists(os.path.join((os.path.join(d, f)),
                        "docker-compose.yml")):
                    continue
                services.append(Service(f, 
                    os.path.normpath(os.path.join(d, f))))
        if os.path.exists(os.path.join(os.path.expanduser("~"),
                ".docker-services")):
            scan_dir(os.path.join(os.path.expanduser("~"),
                    ".docker-services"))
        if os.path.exists("/usr/share/docker-services"):
            scan_dir("/usr/share/docker-services")
        if os.path.exists("/srv"):
            scan_dir("/srv")
        return services

    def _fix_container_name(self, container_name):
        """ !! BIG HACK !!
            Sometimes docker-compose gives us just a shortened name for a
            container. While I am not fully aware of the algorithm, I assume it
            will usually still be unique. In this function, we try to get back to
            the full unshortened name.
        """
        for container in self.containers:
            if container.name == container_name:
                return container_name
        matched_name = None
        for container in self.containers:
            if container.name.startswith(container_name):
                if matched_name != None:
                    raise RuntimeError("encountered unexpected non-unique " +\
                        "container label")
                matched_name = container.name
        return matched_name

    def _get_running_service_container_names(self):
        """ Get all running containers of the given service.
            Returns a list of container ids.
        """
        running_containers = []
        try:
            output = subprocess.check_output([
                SystemInfo.docker_compose_path(), "ps"],
                cwd=self.service_path, stderr=subprocess.STDOUT,
                timeout=10).\
                decode("utf-8", "ignore")
        except subprocess.CalledProcessError as e:
            output = e.output.decode("utf-8", "ignore")
            if output.find("client and server don't have same version") >= 0:
                print_msg("error: it appears docker-compose is " +\
                    "installed with " +\
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
        return [self._fix_container_name(container) \
            for container in running_containers]

    def _internal_docker_compose_links(self, container_name):
        """ Attempt to parse and return all containers referenced as external
            links used by a service from the respective docker-compose.yml of
            that service.
        """
        links = []
        with open(os.path.join(self.service_path,
                "docker-compose.yml"), "rb") as f:
            contents = f.read().decode("utf-8", "ignore").\
                replace("\r\n", "\n").\
                replace("\r", "\n")
        try:
            parsed_obj = yaml.safe_load(contents)
            if not container_name in parsed_obj:
                raise ValueError("no such container found: " +\
                    str(container_name))
            if not "links" in parsed_obj[container_name]:
                return []
            return [entry.partition(":")[0] for entry in \
                parsed_obj[container_name]["links"]]
        except NameError:
            # Continue with the YAML parsing hack:
            pass
        in_relevant_container_section = False
        in_links_list = False
        for line in contents.split("\n"):
            if line.strip() == "":
                continue

            # Figure out whether we are entering/leaving the relevant container
            # section:
            if line.startswith(container_name + ":"):
                in_relevant_container_section = True
            elif not line.startswith(" ") and \
                    not line.startswith("\t") and \
                    line.endswith(":"):
                in_relevant_container_section = False

            # Detect external_links: subsection:
            if line.startswith(" ") and (line.strip().\
                    startswith("links ")
                    or line.strip().startswith("links:")):
                in_links_list = True

                # Parse remaining stuff in line:
                i = line.find("links")
                line = line[i+len("links"):].strip()
                if line.startswith(":"):
                    line = line[1:].strip()
                if len(line) == 0:
                    continue

            # Parse entries:
            if in_links_list:
                if not line.strip().startswith("-"):
                    in_links_list = False
                    continue
                line = line[line.find("-")+1:].strip()
                if line.startswith("\"") and line.endswith("\""):
                    line = line[1:-1].strip()
                parts = line.split(":")
                if in_relevant_container_section:
                    links.append(parts[0])
        return links


    def _external_docker_compose_links(self, container_name):
        """ Attempt to parse and return all containers referenced as external
            links used by a service from the respective docker-compose.yml of
            that service.
        """
        external_links = []
        with open(os.path.join(self.service_path,
                "docker-compose.yml"), "rb") as f:
            contents = f.read().decode("utf-8", "ignore").\
                replace("\r\n", "\n").\
                replace("\r", "\n")
        try:
            parsed_obj = yaml.safe_load(contents)
            if not container_name in parsed_obj:
                raise ValueError("no such container found: " +\
                    str(container_name))
            if not "external_links" in parsed_obj[container_name]:
                return []
            return [entry.partition(":")[0] for entry in \
                parsed_obj[container_name]["external_links"]]
        except NameError:
            # Continue with the YAML parsing hack:
            pass
        in_relevant_container_section = False
        in_external_links_list = False
        for line in contents.split("\n"):
            if line.strip() == "":
                continue

            # Figure out whether we are entering/leaving the relevant container
            # section:
            if line.startswith(container_name + ":"):
                in_relevant_container_section = True
            elif not line.startswith(" ") and \
                    not line.startswith("\t") and \
                    line.endswith(":"):
                in_relevant_container_section = False

            # Detect external_links: subsection:
            if line.startswith(" ") and (line.strip().\
                    startswith("external_links ")
                    or line.strip().startswith("external_links:")):
                in_external_links_list = True

                # Parse remaining stuff in line:
                i = line.find("external_links")
                line = line[i+len("external_links"):].strip()
                if line.startswith(":"):
                    line = line[1:].strip()
                if len(line) == 0:
                    continue

            # Parse entries:
            if in_external_links_list:
                if not line.strip().startswith("-"):
                    in_external_links_list = False
                    continue
                line = line[line.find("-")+1:].strip()
                if line.startswith("\"") and line.endswith("\""):
                    line = line[1:-1].strip()
                parts = line.split(":")
                if in_relevant_container_section:
                    external_links.append(parts[0])
        return external_links

    def get_running_containers(self):
        """ Get only the containers of this service which are currently up and
            running.
        """
        running_names = self._get_running_service_container_names()
        running_containers = []
        for container in self.containers:
            if container.name in running_names:
                running_containers.append(container)
        return running_containers

    @property
    def rw_volumes(self):
        volume_set = set()
        for container in self.containers:
            for volume in container.rw_volumes:
                volume_set.add(volume)
        return list(volume_set)

    @staticmethod
    def clean_up():
        """ This function will check the status of all docker containers, and then
            irrevocably delete all containers that aren't running.
        """
        print_msg("cleaning up stopped containers...")
        output = subprocess.check_output([SystemInfo.docker_path(), "ps", "-a"])
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
                subprocess.check_output([SystemInfo.docker_path(), "rm", parts[0]])
        print_msg("cleaning up unneeded images...")
        subprocess.check_output([SystemInfo.docker_path(), "rmi",
            "$(docker images -aq"], shell=True)
        print_msg("cleaning up dangling volumes...")
        dangling_vols = subprocess.check_output([SystemInfo.docker_path(),
            "volume", "ls", "-qf", "dangling=true"])
        for vol in dangling_vols.splitlines():
            vol = vol.strip()
            if len(vol) == 0:
                continue
            subprocess.check_output([SyStemInfo.docker_path(), "volume", "rm", vol])

    @property
    def containers(self):
        """ Get all containers specified for the given service's
            docker-compose.yml.
        """
        with open(os.path.join(self.service_path,
                "docker-compose.yml"), "rb") as f:
            contents = f.read().decode("utf-8", "ignore").\
                replace("\r\n", "\n").\
                replace("\r", "\n").replace("\t", " ")

        # See if YAML parsing works (depending on pyyaml being installed):
        parsed_obj = None
        try:
            parsed_obj = yaml.safe_load(contents)
        except NameError:
            pass

        results = []

        # Assemble results if YAML parsing worked:
        if parsed_obj != None:
            for container_name in parsed_obj:
                for property_name in parsed_obj[container_name]:
                    value = parsed_obj[container_name][property_name]
                    if property_name == "image":
                        # This container is constructed from an image:
                        image_name = value

                        # This is the resulting container name:
                        results.append(ServiceContainer(
                            self.name, self.service_path,
                            container_name,
                            image_name=image_name))
                    elif property_name == "build":
                        results.append(ServiceContainer(
                            self.name, self.service_path,
                            container_name))
            return results

        # Parse with our hacky YAML pseudo-parsing:
        smallest_observed_indentation = 999
        def count_indentation(line):
            i = 0
            while i < len(line) and line[i] == " ":
                i += 1
            return i
        current_container_name = None
        i = 0
        for line in contents.split("\n"):
            next_line = None
            i += 1
            if i < len(contents):
                next_line = contents[i]

            if not (line.startswith(" ")):
                # This is the container name:
                current_container_name = line.partition(":")[0].strip().\
                    partition(" ")[0]
            elif count_indentation(line) <= smallest_observed_indentation:
                # This is a line inside the service declaration.
                keyword = line.partition(":")[0].strip().partition(" ")[0]
                value = line.partition(":")[2].strip()
                if len(value) == 0:
                    value = next_line.strip()
                if keyword == "build":
                    # This service build from a directory.
                    # Get the name of the directory:
                    build_path = os.path.normpath(\
                        os.path.join(service_path, value))
                    while build_path.endswith("/"):
                        build_path = build_path[:-1]
                    build_name = os.path.basename(build_path).replace("-", "")
                    
                    # This is the resulting container name:
                    results.append(ServiceContainer(
                        self.name, self.service_path,
                        current_container_name))
                elif keyword == "image":
                    # This container is constructed from an image:
                    image_name = value

                    # This is the resulting container name:
                    results.append(ServiceContainer(
                        self.name, self.service_path,
                        current_container_name,
                        image_name=image_name))
        return results

class FailedLaunchTracker(object):
    def __init__(self):
        self.access_lock = threading.Lock()
        self.contents = set()

    def __len__(self):
        self.access_lock.acquire()
        result = len(self.contents)
        self.access_lock.release()
        return result

    def __contains__(self, item):
        self.access_lock.acquire()
        result = (item in self.contents)
        self.access_lock.release()
        return result

    def add(self, item):
        self.access_lock.acquire()
        self.contents.add(item)
        self.access_lock.release()

class LaunchThreaded(threading.Thread):
    """ A helper to launch a service and wait for the launch only for a
        limited amount of time, and moving the launch into a background
        thread if it takes too long.
    """
    
    def __init__(self, container, failed_launch_tracker=None):
        super().__init__()
        self.container = container
        self.failed_launch_tracker = failed_launch_tracker
        self.path = self.container.service.service_path

    def run(self):
        try:
            # Fix permissions if we have instructions for that:
            perms = Permissions(self.container.service)
            perm_info = perms.get_permission_info_from_yml()
            if ("owner" in perm_info["livedata-permissions"]) \
                    and os.path.exists(os.path.join(
                        self.path, "livedata")):
                print_msg("ensuring file permissions of livedata folder...",\
                    service=self.container.service.name,
                    container=self.container.name,
                        color="blue")
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
            waiting_msg = False
            for dependency in self.container.dependencies:
                if not dependency.container.running:
                    if not waiting_msg:
                        waiting_msg = True
                        print_msg("waiting for dependency to launch: " +\
                            str(dependency),
                            service=self.container.service.name,
                            container=self.container.name,
                            color="yellow")
                    time.sleep(5)
                    while not dependency.container.running:
                        if self.failed_launch_tracker != None:
                            if dependency.container in \
                                    self.failed_launch_tracker:
                                print_msg("launch aborted due to failed " +\
                                    "dependency launch: " +\
                                    str(dependency),
                                    service=self.container.service.name,
                                    color="red")
                                self.failed_launch_tracker.add(
                                    self.container)
                                return
                        time.sleep(5)

            # Launch the service:
            print_msg("launching...", service=self.container.service.name,
                container=self.container.name, color="blue")
            try:
                self.container.launch()
                time.sleep(1)
                if not self.container.running:
                    print_msg("failed to launch. (nothing running after " +\
                        "1 second)",\
                        service=self.container.service.name,
                        container=self.container.name, color="red")
                    if self.failed_launch_tracker != None:
                        self.failed_launch_tracker.add(self.container)
                    return
                print_msg("now running.",
                    service=self.container.service.name,
                    container=self.container.name, color="green")
            except subprocess.CalledProcessError:
                print_msg("failed to launch. (error exit code)",\
                    service=self.container.service.name,
                    container=self.container.name,
                    color="red")
                if self.failed_launch_tracker != None:
                    self.failed_launch_tracker.add(self.container)
            except Exception as e:
                print_msg("failed to launch. (unknown error)",\
                    service=self.container.service.name,
                    container=self.container.name,
                    color="red")
                if self.failed_launch_tracker != None:
                    self.failed_launch_tracker.add(self.container)
                raise e
        except Exception as e:
            print("UNEXPECTED ERROR", file=sys.stderr)
            print("ERROR: " + str(e))
            traceback.print_exc()

    @staticmethod
    def attempt_launch(container, to_background_timeout=5,
            failed_launch_tracker=None):
        """ Launch a given service and wait for it to run for a few seconds.
            If that isn't long enough for it to start running, return
            execution to possibly launch further services while this one is
            still busy launching.
        """

        # Start a new launch thread:
        launch_t = LaunchThreaded(container,
            failed_launch_tracker=failed_launch_tracker)
        launch_t.start()
        
        # Wait for it to complete:
        launch_t.join(to_background_timeout)
        if launch_t.isAlive():
            # This took too long, run in background:
            print_msg("Maximum waiting time exceeded, " + \
                "resuming launch in background.",
                service=container.service.name,
                container=container.name,
                color="yellow")
            return launch_t
        return None

    @staticmethod
    def stop(container):
        """ Stop a service. """
        container.stop()

    @staticmethod
    def wait_for_launches(threads):
        for launch_t in threads:
            if launch_t.isAlive():
                launch_t.join()

class Permissions:
    def __init__(self, service):
        self.service = service

    def get_permission_info_from_yml(self):
        """ Get permission info for the given service
        """
        f = None
        try:
            f = open(os.path.join(self.service.service_path,
                "permissions.yml"), "rb")
        except Exception as e:
            return {"livedata-permissions" : {}}
        perm_dict = dict()
        current_area = None
        try:
            contents = f.read().decode("utf-8", "ignore").\
                replace("\r\n", "\n").\
                replace("\r", "\n").replace("\t", " ")
        finally:
            f.close()

        # Try proper parsing with the YAML parser:
        try:
            parsed_obj = yaml.safe_load(contents)
            for k in parsed_obj:
                print_msg("warning: unrecognized permissions.yml " +\
                        "section: " + str(line),
                        service=self.service.name,
                        color="red")
            if not "livedata-permissions" in parsed_obj:
                parsed_obj["livedata-permissions"] = dict()
            return parsed_obj
        except NameError:
            pass

        # Hack for unavailable YAML parser:
        for line in contents.split("\n"):
            if line.strip() == "":
                continue
            if not line.startswith(" ") and not line.startswith("\t"):
                line = line.strip()
                if not line.endswith(":"):
                    print_msg("error: syntax error in permissions.yml: " +\
                        "colon expected",
                        service=self.service.name, color="red")
                    raise RuntimeError("invalid syntax")
                line = line[:-1]
                # this specifies the current area
                if line == "livedata-permissions":
                    current_area = "livedata-permissions"
                else:
                    print_msg("warning: unrecognized permissions.yml " +\
                        "section: " + str(line),
                        service=self.service.name,
                        color="red")
                    continue
                if not current_area in perm_dict:
                    perm_dict[current_area] = dict()
                continue
            elif line.startswith(" ") or line.startswith("\t"):
                if current_area == None:
                    print_msg("error: syntax error in permissions.yml: " +\
                        "unexpected value outside of block",
                        service=self.service.name, color="red")
                    raise RuntimeError("invalid syntax")
                k = line.partition(":")[0].strip()
                v = line.partition(":")[2].strip()
                perm_dict[current_area][k] = v

                continue
        
        # Make sure some stuff is present:
        if not "livedata-permissions" in perm_dict:
            perm_dict["livedata-permissions"] = dict()

        return perm_dict

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

ensure_docker = SystemInfo.docker_path()
ensure_docker_compose = SystemInfo.docker_compose_path()

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

class Snapshots(object):
    def __init__(self, service):
        self.service = service

    def check_running_snapshot_transaction(self):
        if os.path.exists(os.path.join(
                self.service.service_path, ".docker-services-snapshot.lock")):
            output = subprocess.check_output(
                "ps aux | grep docker-services | grep -v grep | wc -l",
                shell=True).decode("utf-8", "ignore")
            if output.strip() != "1":
                # another copy still running??
                return True
            print_msg("warning: stale snapshot lock found but no process " +\
                "appears to be left around, removing.",
                color="yellow", service=self.service.name)
            # no longer running, remove file:
            os.remove(os.path.join(self.service.service_path,
                ".docker-services-snapshot.lock"))
        return False

    @staticmethod
    def btrfs_tool_check():
        # make sure the btrfs tool is working:
        if SystemInfo.btrfs_path():
            print_msg("error: btrfs tool not found. Are btrfs-progs installed?",
                color="red")
            sys.exit(1)
        output = None
        try:
            output = subprocess.check_output([SystemInfo.btrfs_path(),
                "--version"],
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

    def subvolume_readiness_check(self):
        """ Check if the given service is ready for snapshotting or still needs
            btrfs subvolume conversion. Print a warning if not.
        """
        fs = SystemInfo.filesystem_type_at_path(
            self.service.service_path)
        if fs != "btrfs":
            return

        btrfs_tool_check()

        if os.path.exists(os.path.join(service_path, "livedata")):
            if not SystemInfo.is_btrfs_subvolume(os.path.join(
                    self.service.service_path, "livedata"))\
                    and len(self.service.rw_volumes) > 0:
                if self.service.is_running():
                    print_msg("the livedata/ dir of this service will " +\
                        "still need to be converted to a subvolume to " +\
                        "enable snapshots.\n" + \
                        "Fix it by doing this:\n" + \
                        "1. Stop the service with: docker-services.py stop " +\
                            self.service.name + "\n" + \
                        "2. Snapshot the service once with: docker-services.py "+\
                            "snapshot " + self.service.name + "\n",
                        service=self.service.name, color="yellow")
                    return
                else:
                    print_msg("the livedata/ dir of this service still " +\
                        "needs conversion to btrfs subvolume.\n" +\
                        "Fix it by snapshotting it once with: " +\
                        "docker-services.py "+\
                        "snapshot " + service_name + "\n",
                        service=self.service.name, color="yellow")
                    return
        # Everything seems fine so far.
        return

    def do(self):
        """ Make a backup of the live data of the service. """

        self.btrfs_tool_check()

        # Make sure no snapshot is already in progress:
        if self.check_running_snapshot_transaction():
            print_msg("error: snapshot already in progress. " +\
                "try again later", service=self.service.name,
                color="red")
            print_msg("remove .docker-services-snapshot.lock if that is " +\
                "incorrect", service=self.service.name)
            return False

        print_msg("considering for snapshot...",
            service=self.service.name, color="blue")

        # Check which volumes this service has:
        volumes = get_service_volumes(directory, service, rw_only=True)
        if len(volumes) == 0:
            print_msg("service has no read-write volumes, nothing to do.",
                service=self.service.name, color="blue")
            return True
        
        # Check if we have livedata/:
        if not os.path.exists(os.path.join(
                self.service.name, "livedata")):
            print_msg("error: service has read-write volumes, " + \
                "but no livedata/ " +\
                "folder. fix this to enable snapshots",
                service=self.service.name,
                color="red")
            return False

        # Check if we have any volumes which are actually in livedata/:
        empty_snapshot = True
        for volume in volumes:
            relpath = os.path.relpath(
                os.path.realpath(volume),
                os.path.realpath(os.path.join(
                self.service.name, "livedata")),
            )
            if relpath.startswith(os.pardir + os.sep):
                # volume is not in livedata/!
                print_msg("warning: volume " + str(volume) + \
                    " is NOT in livedata/ - " +\
                    "not covered by snapshot!",
                    service=self.service.name, color="yellow")
            else:
                empty_snapshot = False
        if empty_snapshot:
            print_msg("this snapshot would be empty because no read-write " +\
                "volumes are mounted to livedata/ - skipping.",
                service=self.service.name, color="blue")
            return True

        # Check if filesystem of livedata/ is actually btrfs:
        fs = SystemInfo.filesystem_type_at_path(
            os.path.join(self.service.name, "livedata"))
        if fs != "btrfs":
            print_msg("error: livedata/ has other filesystem " + str(fs) + \
                ", should be btrfs!")
            return fs

        livedata_renamed_dir = os.path.join(
            self.service.service_path, ".livedata-predeletion-renamed")
        livedata_dir = os.path.join(
            self.service.service_path, "livedata")
        snapshot_dir = os.path.join(
            self.service.service_path, ".btrfs-livedata-snapshot")
        tempvolume_dir = os.path.join(
            self.service.service_path, ".btrfs-livedata-temporary-volume")
        tempdata_dir = os.path.join(
            self.service.service_path, ".livedata-temporary-prevolume-copy")

        # Make sure the livedata/ dir is a btrfs subvolume:    
        if self.service.is_running():
            if not SystemInfo.is_btrfs_subvolume(livedata_dir):
                print_msg("error: can't do btrfs subvolume conversion because "+\
                    "service is running. The first snapshot is required to " +\
                    "be done when the service is stopped.",
                    service=self.service.name, color="red")
                return False

        lock_path = os.path.join(self.service.service_path,
            ".docker-services-snapshot.lock")

        # Add a transaction lock:
        transaction_id = str(uuid.uuid4())
        with open(lock_path, "wb") as f:
            f.write(transaction_id.encode("utf-8"))
        
        # Wait a short amount of time so other race condition writes will
        # be finished with a very high chance:
        time.sleep(0.5)

        # Verify we got the transaction lock:
        contents = None
        with open(lock_path, "rb") as f:
            contents = f.read().decode("utf-8", "ignore")
        if contents.strip() != transaction_id:
            print_msg("error: mid-air snapshot collision detected!! " + \
                "Did you call the script twice?",
                service=self.service.name, color="red")
            return False

        # Make sure the .livedata-predeletion-renamed isn't there:
        if os.path.exists(livedata_renamed_dir):
            if not os.path.exists(livedata_dir):
                print_msg("warning: .livedata-predeletion-renamed/ " + \
                    "is present and no livedata/ folder." +\
                    "Moving it back...",
                    service=self.service.name, color="yellow")
                shutil.move(livedata_renamed_dir, livedata_dir)
                assert(not os.path.exists(livedata_renamed_dir))
            else:
                print_msg("error: .livedata-predeletion-renamed/ " + \
                    "is still there, indicating a previously aborted " +\
                    "run, but livedata/ is also still around. " +\
                    "Please figure out which one you want to keep, and " +\
                    "delete one of the two.", service=self.service.name,
                    color="red")
                sys.exit(1)

        # Make sure the .livedata-temporary-prevolume-copy directory is unused:
        if os.path.exists(tempdata_dir):
            print_msg("warning: .livedata-temporary-prevolume-copy/ " + \
                "already present! " +\
                "This is probably a leftover from a previously " + \
                "aborted attempt. Will now attempt to delete it...",
                service=self.service.name, color="yellow")
            shutil.rmtree(tempdata_dir)
            assert(not os.path.exists(tempdata_dir))

        # Make sure the btrfs snapshot path is unused:
        if os.path.exists(snapshot_dir):
            if SystemInfo._btrfs_subvolume_stat_check(snapshot_dir):
                print_msg("warning: .btrfs-livedata-snapshot/ " \
                    + "already present! " \
                    + "This is probably a leftover from a previously " + \
                    "aborted attempt. Will now attempt to delete it...",
                    service=self.service.name, color="yellow")
                subprocess.check_output([SystemInfo.btrfs_path(),
                    "subvolume",
                    "delete", snapshot_dir])
                assert(not os.path.exists(snapshot_dir))
            else:
                print_msg("error: .btrfs-livedata-snapshot/ already " +\
                    "present, " \
                    + "but it is not a btrfs snapshot!! I don't know how " +\
                    "to deal with this, aborting.",
                    service=self.service.name, color="red")
                return False

        # Make sure the temporary btrfs subvolume path is unused:
        if os.path.exists(tempvolume_dir):
            print_msg("warning: .btrfs-livedata-temporary-volume/ already " +\
                "present! " \
                + "This is probably a leftover from a previously " + \
                "aborted attempt. Will now attempt to delete it...",
                service=self.service.name, color="yellow")
            output = subprocess.check_output([SystemInfo.btrfs_path(),
                "subvolume",
                "delete", tempvolume_dir])
            assert(not os.path.exists(tempvolume_dir))

        # If this isn't a btrfs subvolume, we will need to fix that first:
        if not SystemInfo.is_btrfs_subvolume(livedata_dir):
            print_msg("warning: initial subvolume conversion required. "+\
                "DON'T TOUCH livedata/ WHILE THIS HAPPENS!!",
                service=self.service.name, color="yellow")
            try:
                output = subprocess.check_output([SystemInfo.btrfs_path(),
                    "subvolume",
                    "create", tempvolume_dir])
            except Exception as e:
                os.remove(lock_path)
                raise e
            assert(btrfs_is_subvolume(tempvolume_dir))

            # Copy all contents:
            assert(not os.path.exists(tempdata_dir))
            shutil.copytree(livedata_dir, tempdata_dir, symlinks=True)
            assert(os.path.exists(tempdata_dir))
            for f in os.listdir(tempdata_dir):
                orig_path = os.path.join(tempdata_dir, f)
                new_path = os.path.join(tempvolume_dir, f)
                shutil.move(orig_path, new_path)

            # Do a superficial check if we copied all things:
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
                    "during the process??",
                    service=self.service.name, color="red")
                return False

            # Remove old livedata/ dir:
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
                service=self.service.name)

        snapshots_dir = os.path.join(self.service.service_path,
            "livedata-snapshots")

        # Create livedata-snapshots/ if not present:
        if not os.path.exists(snapshots_dir):
            os.mkdir(snapshots_dir)

        # Go ahead and snapshot:
        print_msg("initiating btrfs snapshot...",
            service=self.service.name)
        output = subprocess.check_output([SystemInfo.btrfs_path(),
            "subvolume", "snapshot",
            "-r", "--", livedata_dir, snapshot_dir])
        
        # Copy snapshot to directory:
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
            service=self.service.name)
        shutil.copytree(snapshot_dir, snapshot_specific_dir, symlinks=True)
        subprocess.check_output([SystemInfo.btrfs_path(), "subvolume",
            "delete", snapshot_dir])
        assert(not os.path.exists(snapshot_dir))
        print_msg("snapshot complete.", service=self.service.name,
            color="green")

class TargetsParser(object):
    @staticmethod
    def get_containers(targets, print_error=False):
        while targets.find("  ") >= 0:
            targets.replace("  ", " ")
        result = set()
        targets = targets.strip()

        # Go through all targets in the list:
        for target in targets.split(" "):
            # Specific treatment of the "all" keyword:
            if target == "all":
                for service in Service.all():
                    for container in service.containers:
                        result.add(container)
                return list(result)

            # Examine the current service/container entry:
            service_name = target.partition("/")[0]
            service = Service.find_by_name(service_name)
            if service == None:
                if print_error:
                    print("docker-services.py: error: " + \
                        "no such service found: " + str(service_name),
                        file=sys.stderr)
                return None

            # See if a specific container is specified or jst all of them:
            container_name = target.partition("/")[2]
            if len(container_name) == 0: # all containers:
                containers = service.containers
                if len(containers) == 0:
                    print_msg("warning: specified service has no " +\
                        "containers", color="yellow", service=service.name)
            else: # a specific container. find it by name:
                containers = []
                for service_container in service.containers:
                    if service_container.name == container_name:
                        containers = [ service_container ]
                        break
                # Check if the container was found by name or not:
                if len(containers) == 0:
                    if print_error:
                        print("docker-services.py: error: " + \
                            "no such container for service \"" +\
                            str(service_name) + "\" found: " +\
                            str(container_name),
                            file=sys.stderr)
                    return None
            # Add all containers collected by this entry:
            for container in containers:
                result.add(container)
        return list(result)

    @staticmethod
    def get_services(self, print_error=False):
        while targets.find("  ") >= 0:
            targets.replace("  ", " ")
        result = set()
        targets = targets.strip()

        # Go through all targets in the list:
        for target in targets.split(" "):
            # Specific treatment of the "all" keyword:
            if target == "all":
                for service in Service.all():
                    result.add(service)
                return list(result)
            service = Service.find_by_name(service_name)
            if service == None:
                if print_error:
                    print("docker-services.py: error: " + \
                        "no such service found: " + str(service_name),
                        file=sys.stderr)
                return None
            result.add(service)
        return list(result)

def unknown_action(hint=None):
    """ Print an error that the given action to docker-services.py is invalid,
        with a possible hint to suggest another action.
    """
    print("docker-services.py: error: unknown action: " + \
        args.action, file=sys.stderr)
    if hint != None:
        print("Did you mean: " + str(hint) + "?")
    sys.exit(1)

# Ensure the docker main service is running:
error_output = None
try:
    subprocess.check_output([SystemInfo.docker_path(), "ps"],
        stderr=subprocess.STDOUT)
except subprocess.CalledProcessError as e:
    error_output = e.output.decode("utf-8", "ignore")
if error_output != None:
    # Old-style error message:
    if error_output.find("dial unix") >= 0 and \
            error_output.find("no such file or directory") >= 0:
        print("docker-services.py: error: " + \
            "docker daemon appears to be not running." +\
            " Please start it and ensure it is reachable.")
        sys.exit(1)
    # Newer error message:
    elif error_output.find("Cannot connect to the Docker daemon") >= 0:
        print("docker-services.py: error: " + \
            "docker daemon appears to be not running." +\
            " Please start it and ensure it is reachable.")
        sys.exit(1)
    else:
        print("docker-services.py: error: " + \
            "there appears to be some unknown problem with " + \
            "docker! (test run of \"docker ps\" returned error code)")
        sys.exit(1)

# Check if services are btrfs ready, and give warning if not:
if args.action != "snapshot":
    for service in Service.all():
        snapshots = Snapshots(service)
        snapshots.subvolume_readiness_check()

# --- Main handling of actions here:

if args.action == "list" or args.action == "ps" or args.action == "status":
    all_services = Service.all()
    print("Service list (" + str(len(all_services)) + " service(s)):")
    for service in all_services:
        state = ""
        if service.is_running():
            state = "\033[1;32mrunning\033[0m"
        else:
            state = "\033[1;31mstopped\033[0m"
        print("\033[0m\033[1m" + service.name + "\033[0m, in: " + \
            service.service_path + ", state: " + state)
elif args.action == "help":
    parser.print_help()
    sys.exit(1)
elif args.action == "logs":
    if len(args.argument) == 0:
        print("docker-services.py: error: please specify the name " + \
            "of the service for which docker logs shold be printed, "+\
            "or \"all\"", file=sys.stderr)
        sys.exit(1)
    containers = TargetsParser.get_containers(" ".join(args.argument),
        print_error=True)
    if containers == None:
        sys.exit(1)
    for container in containers:
        print_msg("printing log of container " + str(container),
            service=container.service.name, color='blue')
        try:
            retcode = subprocess.call([SystemInfo.docker_path(), "logs",
                    container.current_running_instance_name],
                    stderr=subprocess.STDOUT)
            if retcode != 0:
                raise subprocess.CalledProcessError(
                    retcode, " ".join([SystemInfo.docker_path(), "logs"]))
        except subprocess.CalledProcessError:
            print_msg("failed printing logs. " +\
                "Maybe container has no logs yet?",
                service=container.service.name,
                container=container.name, color='yellow')
            pass
elif args.action == "shell":
    if len(args.argument) == 0:
        print("docker-services.py: error: please specify the name " + \
            "of the service for which an interactive shell should be started",
            file=sys.stderr)
        sys.exit(1)
    containers = TargetsParser.get_containers(" ".join(args.argument),
        print_error=True)
    if containers == None:
        sys.exit(1)
    if len(containers) != 1:
        print("docker-services.py: error: this command can only be used " + \
            "on a single container/service. It matches " + str(containers) +\
            " containers: " +\
            ", ".join([container.name for container in containers]),
            file=sys.stderr)
        sys.exit(1)
    if containers[0].running:
        cname = containers[0].current_running_instance_name
        print_msg("attaching to running container " + str(cname),
            service=containers[0].service.name,
            color="blue")
        subprocess.call([SystemInfo.docker_path(), "exec", "-t", "-i",
                str(cname), "/bin/bash"],
            stderr=subprocess.STDOUT)
    else:
        print_msg("launching container " + str(containers[0]) + \
            " with shell",
            service=containers[0].service.name,
            color="blue")
        image_name = containers[0].image_name
        subprocess.call([SystemInfo.docker_compose_path(), "build",
            containers[0].name], cwd=containers[0].service.service_path)
        subprocess.call([SystemInfo.docker_compose_path(), "run",
            image_name, "/bin/bash"], cwd=containers[0].service.service_path)
elif args.action == "start" or args.action == "restart":
    if len(args.argument) == 0:
        print("docker-services.py: error: please specify the name " + \
            "of the service to be started, or \"all\"", file=sys.stderr)
        sys.exit(1)
    containers = TargetsParser.get_containers(" ".join(args.argument),
        print_error=True)
    if len(containers) == 0:
        sys.exit(0)

    # This will hold all the containers that need to be (re)started including
    # the dependencies they need themselves:
    stop_containers = list()
    if args.action == "restart":
        stop_containers = copy.copy(containers)
    start_containers = copy.copy(containers)

    # This will hold all the containers depending on the restarted ones that
    # also need to be restarted as a consequence:
    restart_dependant_containers = list()
    
    # Collect dependencies of the containers to be (re)started to ensure they
    # will be started too:
    list_changed = True
    while list_changed:
        list_changed = False
        for container in containers:
            for dep in container.dependencies:
                dep_container = None
                try:
                    dep_container = dep.container
                except ValueError:
                    print("docker-services.py: error: dependency for " +\
                        str(container) + " is not in known services, " +\
                        "can't resolve dependency chain: " +\
                        str(dep), file=sys.stderr)
                    sys.exit(1)
                if not dep_container in start_containers:
                    start_containers.append(dep_container)
                    list_changed = True

    # Collect services depending on the containers that are restarted in the
    # process, so they can be restarted too:
    list_changed = True
    while list_changed:
        list_changed = False
        for service in Service.all():
            for container in service.containers:
                for stopped_container in (stop_containers +\
                        restart_dependant_containers):
                    for dep in container.dependencies:
                        dep_container = None
                        try:
                            dep_container = dep.container
                        except ValueError:
                            pass
                        if dep_container == stopped_container and \
                                not container in stop_containers:
                            if not container in restart_dependant_containers:
                                list_changed = True
                                restart_dependant_containers.append(container)

    tracker = FailedLaunchTracker()

    #print("stop_containers: " + str(stop_containers))
    #print("restart_dependant_containers: " + str(
    #    restart_dependant_containers))
    #print("start_containers: " + str(start_containers))

    # Handle actual (re)starts:
    threads = list()
    for container in stop_containers:
        print_msg("stopping... (for scheduled restart)",
            service=container.service.name, container=container.name,
            color="blue")
        LaunchThreaded.stop(container)
    for container in restart_dependant_containers:
        print_msg("stopping... (container depending on 1+ " +\
            "restarted container(s))",
            service=container.service.name, container=container.name,
            color="blue")
        LaunchThreaded.stop(container)
    for container in start_containers + restart_dependant_containers:
        if container.running:
            print_msg("already running.", service=container.service.name,\
                container=container.name,
                color="green")
        t = LaunchThreaded.attempt_launch(container,
            failed_launch_tracker=tracker)
        if t != None:
            threads.append(t)
    LaunchThreaded.wait_for_launches(threads)
    if len(tracker) > 0:
        print("docker-services.py: error: some launches failed.",
            file=sys.stderr)
        sys.exit(1)
    else:
        sys.exit(0)
elif args.action == "stop":
    if len(args.argument) == 0:
        print("docker-services.py: error: please specify the name " + \
            "of the service to be stopped, or \"all\"", file=sys.stderr)
        sys.exit(1)
    containers = TargetsParser.get_containers(" ".join(args.argument),
        print_error=True)
    for container in containers:
        if container.running:
            print_msg("stopping...", service=container.service.name,
                container=container.name,
                color="blue")
            LaunchThreaded.stop(container)
            print_msg("stopped.", service=container.service.name,
                container=container.name, color="green")
        else:
            print_msg("not currently running.",
                service=container.service.name,
                container=container.name,
                color="blue")
    sys.exit(0)
elif args.action == "snapshot":
    if len(args.argument) == 0:
        print("docker-services.py: error: please specify the name " + \
            "of the service to be snapshotted, or \"all\"", file=sys.stderr)
        sys.exit(1)
    services = TargetsParser.get_services(" ".join(args.argument),
        print_error=True)
    return_error = False
    for service in services:
        fs = SystemInfo.filesystem_type_at_path(service.service_path)
        if fs != "btrfs":
            print_msg("cannot snapshot service. filesystem " +\
                "is " + fs + ", would need to be btrfs",
                service=service.name, color="red")
            return_error = True
            continue
        snapshots = Snapshots(service)
        if not snapshots.do():
            return_error = True
    if return_error:
        print("docker-services.py: error: some snapshots failed.",
            file=sys.stderr)
        sys.exit(1)
    else:
        sys.exit(0)
elif args.action == "clean":
    clean_up()
else:
    unknown_action()
