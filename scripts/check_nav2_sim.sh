#!/bin/bash
set -eo pipefail
source /opt/ros/lyrical/setup.bash
set -u

missing=0
for package in nav2_bringup nav2_minimal_tb3_sim ros_gz_sim nav2_msgs; do
  if ros2 pkg prefix "$package" >/dev/null 2>&1; then
    printf 'OK   %s\n' "$package"
  else
    printf '缺少 %s\n' "$package"
    missing=1
  fi
done

if command -v gz >/dev/null 2>&1; then
  printf 'OK   gz (%s)\n' "$(command -v gz)"
else
  echo '缺少 Gazebo Sim 命令 gz'
  missing=1
fi

if [ "$missing" -ne 0 ]; then
  echo '请先运行 bash scripts/install_nav2_sim.sh'
  exit 1
fi

echo 'Nav2/Gazebo 仿真依赖完整。'
