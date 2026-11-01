#!/usr/bin/env python3
"""Launch the shared warehouse with two isolated Nav2/TurtleBot3 stacks."""

import os
from pathlib import Path
import sys
import tempfile

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription, LaunchService
from launch.actions import (
    AppendEnvironmentVariable,
    ExecuteProcess,
    IncludeLaunchDescription,
    OpaqueFunction,
    RegisterEventHandler,
)
from launch.conditions import IfCondition
from launch.event_handlers import OnShutdown
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import Command, TextSubstitution
from launch.substitutions.find_executable import FindExecutable
from launch_ros.actions import Node


ROOT = Path(__file__).resolve().parents[2]
ROBOTS = (
    ("robot_1", -2.0, -0.5, 0.0),
    ("robot_2", -2.0, 0.5, 0.0),
)


def generate_launch_description():
    bringup = Path(get_package_share_directory("nav2_bringup"))
    sim = Path(get_package_share_directory("nav2_minimal_tb3_sim"))
    ros_gz_sim = Path(get_package_share_directory("ros_gz_sim"))
    world = ROOT / "simulation/worlds/rdl_warehouse.sdf.xacro"
    warehouse_map = ROOT / "simulation/maps/rdl_warehouse.yaml"
    params = ROOT / ".runtime/nav2_params.yaml"
    graph = bringup / "graphs/turtlebot3_graph.geojson"
    robot_sdf = sim / "urdf/gz_waffle.sdf.xacro"
    robot_description = (sim / "urdf/turtlebot3_waffle.urdf").read_text()
    bridge_with_clock = sim / "configs/turtlebot3_waffle_bridge.yaml"
    bridge_without_clock = ROOT / "simulation/config/tb3_bridge_no_clock.yaml"
    native_gui = os.environ.get("RDL_NATIVE_GUI") == "1"

    world_sdf = tempfile.mktemp(prefix="rdl_multi_", suffix=".sdf")
    actions = [
        AppendEnvironmentVariable("GZ_SIM_RESOURCE_PATH", str(sim / "models")),
        AppendEnvironmentVariable("GZ_SIM_RESOURCE_PATH", str(sim.parent)),
        ExecuteProcess(
            cmd=[
                "xacro", "-o", world_sdf,
                f"headless:={'false' if native_gui else 'true'}", str(world),
            ],
            output="screen",
        ),
        ExecuteProcess(cmd=["gz", "sim", "-r", "-s", world_sdf], output="screen"),
        RegisterEventHandler(
            OnShutdown(on_shutdown=[OpaqueFunction(function=lambda _: os.remove(world_sdf))])
        ),
    ]
    if native_gui:
        actions.append(
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(str(ros_gz_sim / "launch/gz_sim.launch.py")),
                launch_arguments={"gz_args": "-v4 -g"}.items(),
            )
        )

    for index, (name, x, y, yaw) in enumerate(ROBOTS):
        namespace = TextSubstitution(text=name)
        actions.extend([
            Node(
                package="ros_gz_bridge",
                executable="parameter_bridge",
                namespace=name,
                parameters=[{
                    "config_file": str(
                        bridge_with_clock if index == 0 else bridge_without_clock
                    ),
                    "expand_gz_topic_names": True,
                    "use_sim_time": True,
                }],
                output="screen",
            ),
            Node(
                package="ros_gz_sim",
                executable="create",
                namespace=name,
                output="screen",
                arguments=[
                    "-name", name,
                    "-string", Command([
                        FindExecutable(name="xacro"), " ", "namespace:=", name,
                        " ", str(robot_sdf),
                    ]),
                    "-x", str(x), "-y", str(y), "-z", "0.01", "-Y", str(yaw),
                ],
            ),
            Node(
                package="robot_state_publisher",
                executable="robot_state_publisher",
                name="robot_state_publisher",
                namespace=name,
                parameters=[{"use_sim_time": True, "robot_description": robot_description}],
                remappings=[("/tf", "tf"), ("/tf_static", "tf_static")],
                output="screen",
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(str(bringup / "launch/bringup_launch.py")),
                launch_arguments={
                    "namespace": namespace,
                    "slam": "False",
                    "use_localization": "True",
                    "serve_static_map": "True",
                    "map": str(warehouse_map),
                    "graph": str(graph),
                    "use_sim_time": "True",
                    "params_file": str(params),
                    "autostart": "True",
                    "use_composition": "True",
                    "use_intra_process_comms": "False",
                    "use_respawn": "False",
                    "use_keepout_zones": "False",
                    "use_speed_zones": "False",
                    "container_name": "nav2_container",
                }.items(),
            ),
        ])
        if native_gui:
            actions.append(
                IncludeLaunchDescription(
                    PythonLaunchDescriptionSource(str(bringup / "launch/rviz_launch.py")),
                    launch_arguments={
                        "namespace": namespace,
                        "use_sim_time": "True",
                        "rviz_config": str(bringup / "rviz/nav2_default_view.rviz"),
                    }.items(),
                    condition=IfCondition("true"),
                )
            )
    return LaunchDescription(actions)


if __name__ == "__main__":
    service = LaunchService(argv=sys.argv[1:])
    service.include_launch_description(generate_launch_description())
    raise SystemExit(service.run())
