#!/bin/bash
set -eo pipefail
source "$(dirname "$0")/env.bash"
cd "$RDL_ROOT/ros_ws"
colcon build --parallel-workers 2
