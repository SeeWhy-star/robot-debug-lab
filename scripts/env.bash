#!/bin/bash
source /opt/ros/lyrical/setup.bash
RDL_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$RDL_ROOT:${PYTHONPATH:-}"
export ROS_DOMAIN_ID=${RDL_DOMAIN_ID:-93}
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
