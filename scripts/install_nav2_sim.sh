#!/bin/bash
set -eo pipefail
source /opt/ros/lyrical/setup.bash
set -u

echo "将安装 ROS 2 Lyrical 的 Nav2、Gazebo Sim 和官方 TurtleBot3 仿真。"
sudo apt-get update
sudo apt-get install -y ros-lyrical-nav2-bringup

ros2 pkg prefix nav2_bringup >/dev/null
ros2 pkg prefix nav2_minimal_tb3_sim >/dev/null
ros2 pkg prefix ros_gz_sim >/dev/null
command -v gz >/dev/null

echo "Nav2/Gazebo 依赖已安装。运行 bash scripts/check_nav2_sim.sh 验证。"
