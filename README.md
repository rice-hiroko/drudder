
# What is this

## Motivation

You have multiple complex services, each consisting of 1+ Dockerfiles
and its docker-compose.yml which launches it. You suddenly discover you
need a nice way to launch and manage all of these services in one go!


## Features

- easily start/stop and manage multiple docker-compose controlled services
  in one go

- simple docker-compose.yml grouping of multiple containers to a service
  for easier use

- clean up all stopped containers and dangling unnamed volumes in one go

- creation of atomic snapshots of all your read-write volumes/live data
  without shutting down or pausing your containers (for backup purposes)

- one self-contained file

**This script is an alpha status. Expect some bugs and problems.**

# Basic usage

Usage:

```
  docker-services.py list                - list all known services
  docker-services.py start <service>     - starts the specified service
  docker-services.py stop <service>      - stops the specified service
  docker-services.py restart <service>   - restarts the given service.
                                           WARNING: the containers will
                                           *always* get rebuilt and
                                           recreated by this command.
                                           All data in them outside of
                                           volumes will be reset!!
  docker-services.py logs <service>      - print logs of all docker 
                                           containers of the service
  docker-services.py shell <service>[/<subservice>]  - run a shell in the
                                                       specified
                                                       subservice's
                                                       container
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



Copyright (C) Jonas Thiem, 2015

