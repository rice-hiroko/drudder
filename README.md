
# What is this


## Motivation

**docker-rudder** is a tool which operates on top of docker and
docker-compose. It doesn't add any notable new features (other than btrfs
snapshot handling): instead it tries to offer a well-thought-out interface
to simplify all the required daily tasks needed for managing your docker
containers.

It unifies some tasks requiring multiple commands with the regular docker
interfaces in single quick commands with sensible defaults and satefy
checks.


## Features

- easily start/stop and manage multiple docker-compose controlled
  services in one go

- clean up all stopped containers and dangling volumes

- creation of atomic snapshots of all your read-write volumes/live data
  without shutting down or pausing your containers (for backup purposes)

- one self-contained file

**This script is in an experimental state. Expect some bugs and problems.**



# Basic usage

Usage:

```
  drudder list                - list all known services
  drudder start <service>     - starts the specified service
  drudder stop <service>      - stops the specified service
  drudder restart <service>   - restarts the given service.
                                **WARNING**: the containers will *always*
                                get rebuilt and recreated by this command
                                (unless this would result in dangling
                                volumes).
                                All data in the containers outside of
                                volumes will be reset!
  drudder rebuild <service>   - force rebuild of the service from the
                                newest Dockerfile and/or image. Please note
                                this is only required if you want to force
                                a rebuild from the ground up, the (re)start
                                actions will already update the container 
                                if any of the relevant Dockerfiles were
                                changed.
  drudder status <service>[/subservice] - show extended info about the
                                          service
  drudder logs <service>      - print logs of all docker containers of the
                                service
  drudder shell <service>[/<subservice>]  - run a shell in the specified
                                            subservice's container
  drudder snapshot <service>  - makes a snapshot of the live data if
                                enabled. (optional) This feature requires
                                btrfs
  drudder clean               - deletes all containers that aren't running
                                and all dangling volumes
```

**Hint**: You can always use "all" as service target if you want to apply
an action to all services on your machine.



# Installation

Copy the drudder script to /usr/bin/ and set execution bit (chmod +x)



# HOW TO add your service

docker-rudder expects services grouped with docker-compose /
docker-compose.yml. The script will scan the following locations for
services subfolders with a docker-compose.yml in them:

- the current working directory when running the script
- /usr/share/docker-services/  
- /srv/

Each service folder inside one of those locatoins should contain:

- docker-compose.yml to launch it. (folders without this file are skipped)
- livedata/ subfolder where all read-write volumes are mounted to
                            (recommended, see snapshots as described below)

To list all currently recognized services, type: `drudder list`

Congratulations, you can now manage launch your service(s) with
docker-rudder!



# HOW TO backup

You should backup all your services. docker-rudder provides snapshot
functionality to help with this. While you could simply copy your service
folder with all the mounted volumes in it, this can lead to corrupt copies
when doing this while some services are operating (SQL databases etc.).

To use docker-rudder snapshots of your writable volumes during service
operation, do this:

1. Enable snapshots as described below

2. Always run "docker-rudder snapshot all" before you make your backup
   to get consistent snapshots of your writable volumes in a subfolder
   named livedata-snapshpots/ in each respective service folder.



# HOW TO enable snapshots (optional)

This feature allows you to easily copy all the live data of your read-write
mounted volumes your containers as atomic snapshots even while your
services are running and continue to write data.

The snapshots will be atomic, therefore they should be suitable even for
database realtime operations while the database is running and writing to
the volume(s).


## Enable it for a service

How to enable snapshots for a service:

1. Make sure your services folder is on a btrfs file system (not ext4).

2. Each of your snapshot enabled services needs to have a subfolder
   livedata/ where all read-write volumes of it are located.


## Test/do it

**Before you go into production, make sure to test snapshots for your
service(s) at least once!**

Calling:
   ``` drudder snapshot <service>|"all" ```

will now use btrfs functionality to add a time-stamped folder with an
atomic snapshot of livedata/ of the specified service(s) into a new
livedata-snapshots/ subfolder - while your service can continue using the
volume thanks to btrfs' copy-on-write snapshot functionality.


## Restore a snapshot

You can easily restore such a snapshot by shutting down your service
temporarily, copying back a snapshot into livedata/ and turning your
service back on.



Copyright (C) Jonas Thiem et al., 2015-2016

