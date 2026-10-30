"""ROS 2 Nav2/Gazebo bridge with a bounded laser-data fault boundary.

The simulator publishes /scan.  This bridge forwards it to /scan_faulted,
which is consumed by Nav2 obstacle and collision layers. AMCL stays on the raw
scan so localization remains healthy while navigation loses obstacle data.
Stopping forwarding produces a realistic stale-sensor incident while retaining
ground-truth evidence that Gazebo and the sensor are still alive.
"""

from collections import deque
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time

import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped
from lifecycle_msgs.msg import Transition
from lifecycle_msgs.srv import ChangeState, GetState
from nav_msgs.msg import Odometry
from rcl_interfaces.msg import Parameter as ParameterMessage
from rcl_interfaces.msg import ParameterType, ParameterValue
from rcl_interfaces.srv import GetParameters, SetParameters
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import (
    DurabilityPolicy,
    QoSProfile,
    ReliabilityPolicy,
    qos_profile_sensor_data,
)
from sensor_msgs.msg import LaserScan

from .config import ROOT, RUNTIME


WAREHOUSE_WORLD = ROOT / "simulation" / "worlds" / "rdl_warehouse.sdf.xacro"
WAREHOUSE_MAP = ROOT / "simulation" / "maps" / "rdl_warehouse.yaml"
START_X = -2.0
START_Y = -0.5


def rewrite_nav2_params(text):
    """Guard navigation laser consumers while keeping AMCL localization live."""
    return re.sub(
        r'^(\s*topic:\s*)[\"\']?scan[\"\']?\s*$',
        r"\1scan_faulted",
        text,
        flags=re.MULTILINE,
    )


class Nav2Bridge:
    robot_ids = ("robot_1",)

    def __init__(self):
        try:
            from nav2_msgs.action import ComputePathToPose, NavigateToPose
            from nav2_msgs.srv import ManageLifecycleNodes
        except ImportError as error:
            raise RuntimeError(
                "缺少 Nav2/Gazebo。请运行 bash scripts/install_nav2_sim.sh"
            ) from error

        self.NavigateToPose = NavigateToPose
        self.ComputePathToPose = ComputePathToPose
        self.ManageLifecycleNodes = ManageLifecycleNodes
        rclpy.init(signal_handler_options=rclpy.signals.SignalHandlerOptions.NO)
        self.node = Node(
            "robot_debug_nav2_bridge",
            parameter_overrides=[Parameter("use_sim_time", value=True)],
        )
        self.executor = MultiThreadedExecutor(num_threads=4)
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.thread.start()

        self.data_lock = threading.RLock()
        self.locks = {"robot_1": threading.Lock()}
        self.pose = None
        self.ground_truth = None
        self.trace = deque(maxlen=500)
        self.scan_forwarding = True
        self.scan_raw_times = deque(maxlen=200)
        self.scan_forwarded_times = deque(maxlen=200)
        self.active_goal = None
        self.scenario = None
        self.log_handle = None
        self.sim = None

        self.scan_pub = self.node.create_publisher(
            LaserScan, "/scan_faulted", qos_profile_sensor_data
        )
        self.node.create_subscription(
            LaserScan, "/scan", self._scan, qos_profile_sensor_data
        )
        self.node.create_subscription(
            PoseWithCovarianceStamped, "/amcl_pose", self._pose, 10
        )
        self.node.create_subscription(Odometry, "/odom", self._odom, 10)
        initial_pose_qos = QoSProfile(
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self.initial_pose_pub = self.node.create_publisher(
            PoseWithCovarianceStamped, "/initialpose", initial_pose_qos
        )
        self.client = ActionClient(self.node, NavigateToPose, "/navigate_to_pose")
        self.path_client = ActionClient(
            self.node, ComputePathToPose, "/compute_path_to_pose"
        )
        self.lifecycle_client = self.node.create_client(
            GetState, "/bt_navigator/get_state"
        )
        self.lifecycle_change_client = self.node.create_client(
            ChangeState, "/bt_navigator/change_state"
        )
        self.controller_get_client = self.node.create_client(
            GetParameters, "/controller_server/get_parameters"
        )
        self.controller_set_client = self.node.create_client(
            SetParameters, "/controller_server/set_parameters"
        )
        self.lifecycle_manager_client = self.node.create_client(
            ManageLifecycleNodes, "/lifecycle_manager_nav2/manage_nodes"
        )
        self.controller_defaults = {"vx_max": 0.5, "vx_min": -0.35}

        self.params = self._prepare_nav2_params()
        try:
            self._start_sim()
            self.controller_defaults = self.controller_config()
        except Exception:
            self._stop_sim()
            raise

    @staticmethod
    def _require_robot(robot):
        if robot != "robot_1":
            raise ValueError("Nav2 场景当前只有 robot_1")

    def _prepare_nav2_params(self):
        source = Path("/opt/ros/lyrical/share/nav2_bringup/params/nav2_params.yaml")
        if not source.is_file():
            raise RuntimeError(
                "缺少 Nav2 参数文件，请运行 bash scripts/install_nav2_sim.sh"
            )
        text = rewrite_nav2_params(source.read_text())
        target = RUNTIME / "nav2_params.yaml"
        target.write_text(text)
        return target

    def _spawn_sim(self, params):
        self.log_handle = (RUNTIME / "nav2-simulation.log").open("a")
        env = os.environ.copy()
        headless = "false" if env.get("RDL_NATIVE_GUI") == "1" else "true"
        use_rviz = "true" if env.get("RDL_NATIVE_GUI") == "1" else "false"
        command = [
            "/opt/ros/lyrical/bin/ros2",
            "launch",
            "nav2_bringup",
            "tb3_simulation_launch.py",
            f"headless:={headless}",
            f"use_rviz:={use_rviz}",
            f"params_file:={params}",
            f"world:={WAREHOUSE_WORLD}",
            f"map:={WAREHOUSE_MAP}",
        ]
        return subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def _start_sim(self):
        self.sim = self._spawn_sim(self.params)
        if not self.client.wait_for_server(timeout_sec=45):
            if self.sim.poll() is not None:
                raise RuntimeError(
                    "Nav2/Gazebo 启动失败，请查看 .runtime/nav2-simulation.log"
                )
            raise RuntimeError("Nav2 导航服务在 45 秒内未就绪")
        self._set_initial_pose()
        self._wait_nav2_active()

    def _wait_nav2_active(self, timeout=45):
        """Wait for the action server to be usable, not merely discoverable."""
        if not self.lifecycle_client.wait_for_service(timeout_sec=timeout):
            raise RuntimeError("Nav2 生命周期服务未就绪")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.sim and self.sim.poll() is not None:
                raise RuntimeError(
                    "Nav2/Gazebo 启动失败，请查看 .runtime/nav2-simulation.log"
                )
            try:
                state = self._wait(
                    self.lifecycle_client.call_async(GetState.Request()), timeout=3
                )
                if state.current_state.label == "active":
                    return
            except TimeoutError:
                pass
            time.sleep(0.5)
        raise RuntimeError("Nav2 在限定时间内未进入 active 状态")

    def _stop_sim(self):
        if self.sim and self.sim.poll() is None:
            os.killpg(self.sim.pid, signal.SIGINT)
            try:
                self.sim.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self.sim.pid, signal.SIGKILL)
                self.sim.wait()
        self.sim = None
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None

    def _set_initial_pose(self):
        message = PoseWithCovarianceStamped()
        message.header.frame_id = "map"
        message.pose.pose.position.x = START_X
        message.pose.pose.position.y = START_Y
        message.pose.pose.orientation.w = 1.0
        message.pose.covariance[0] = 0.25
        message.pose.covariance[7] = 0.25
        message.pose.covariance[35] = 0.07
        deadline = time.monotonic() + 15
        while self.pose is None and time.monotonic() < deadline:
            message.header.stamp = self.node.get_clock().now().to_msg()
            self.initial_pose_pub.publish(message)
            time.sleep(0.5)
        if self.pose is None:
            raise RuntimeError(
                "Nav2 已启动但定位未就绪，请查看 .runtime/nav2-simulation.log"
            )

    def _scan(self, message):
        now = time.time()
        with self.data_lock:
            self.scan_raw_times.append(now)
            forwarding = self.scan_forwarding
            if forwarding:
                self.scan_forwarded_times.append(now)
        if forwarding:
            self.scan_pub.publish(message)

    def _pose(self, message):
        p = message.pose.pose
        # yaw is enough for the 2-D dashboard; avoid adding a tf dependency.
        z, w = p.orientation.z, p.orientation.w
        yaw = 2.0 * math.atan2(z, w)
        current = {
            "x": p.position.x,
            "y": p.position.y,
            "theta": yaw,
            "speed": 0.0,
            "observed": time.time(),
        }
        with self.data_lock:
            self.pose = current

    def _odom(self, message):
        """Convert simulator odometry into map coordinates for independent QA."""
        p = message.pose.pose
        z, w = p.orientation.z, p.orientation.w
        current = {
            "x": p.position.x + START_X,
            "y": p.position.y + START_Y,
            "theta": 2.0 * math.atan2(z, w),
            "speed": math.hypot(
                message.twist.twist.linear.x, message.twist.twist.linear.y
            ),
            "observed": time.time(),
            "source": "gazebo_odometry",
        }
        with self.data_lock:
            self.ground_truth = current
            if (
                not self.trace
                or math.hypot(
                    current["x"] - self.trace[-1][0],
                    current["y"] - self.trace[-1][1],
                )
                > 0.04
            ):
                self.trace.append([current["x"], current["y"]])

    @staticmethod
    def _rate(samples, window=2.0):
        cutoff = time.time() - window
        count = sum(1 for stamp in samples if stamp >= cutoff)
        return round(count / window, 1)

    @staticmethod
    def _wait(future, timeout=5):
        event = threading.Event()
        future.add_done_callback(lambda _: event.set())
        if not event.wait(timeout):
            raise TimeoutError("ROS operation timed out")
        if future.exception():
            raise future.exception()
        return future.result()

    def topic_health(self, robot="robot_1"):
        self._require_robot(robot)
        with self.data_lock:
            return {
                "raw_scan_hz": self._rate(tuple(self.scan_raw_times)),
                "nav2_scan_hz": self._rate(tuple(self.scan_forwarded_times)),
                "scan_forwarding": self.scan_forwarding,
                "observed": time.time(),
            }

    def navigator_state(self, robot="robot_1"):
        self._require_robot(robot)
        if not self.lifecycle_client.wait_for_service(timeout_sec=2):
            return {"node": "/bt_navigator", "id": 0, "label": "unavailable"}
        response = self._wait(self.lifecycle_client.call_async(GetState.Request()), 3)
        return {
            "node": "/bt_navigator",
            "id": int(response.current_state.id),
            "label": response.current_state.label,
        }

    def controller_config(self, robot="robot_1"):
        self._require_robot(robot)
        if not self.controller_get_client.wait_for_service(timeout_sec=2):
            return {"vx_max": None, "vx_min": None}
        request = GetParameters.Request()
        request.names = ["FollowPath.vx_max", "FollowPath.vx_min"]
        response = self._wait(self.controller_get_client.call_async(request), 3)
        values = [value.double_value for value in response.values]
        return {"vx_max": values[0], "vx_min": values[1]}

    def _set_controller_config(self, vx_max, vx_min):
        if not self.controller_set_client.wait_for_service(timeout_sec=2):
            raise RuntimeError("controller_server 参数服务不可用")
        request = SetParameters.Request()
        request.parameters = [
            ParameterMessage(
                name=name,
                value=ParameterValue(
                    type=ParameterType.PARAMETER_DOUBLE, double_value=float(value)
                ),
            )
            for name, value in [
                ("FollowPath.vx_max", vx_max),
                ("FollowPath.vx_min", vx_min),
            ]
        ]
        response = self._wait(self.controller_set_client.call_async(request), 4)
        if len(response.results) != 2 or not all(result.successful for result in response.results):
            reasons = [result.reason for result in response.results]
            raise RuntimeError("controller_server 拒绝参数变更：" + "; ".join(reasons))
        return self.controller_config()

    def _change_navigator(self, transition_id, expected):
        if not self.lifecycle_change_client.wait_for_service(timeout_sec=2):
            raise RuntimeError("bt_navigator lifecycle 服务不可用")
        request = ChangeState.Request()
        request.transition = Transition(id=transition_id)
        response = self._wait(self.lifecycle_change_client.call_async(request), 5)
        if not response.success:
            raise RuntimeError("bt_navigator lifecycle 状态变更被拒绝")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.navigator_state()
            if state["label"] == expected:
                return state
            time.sleep(0.1)
        raise RuntimeError(f"bt_navigator 未进入 {expected} 状态")

    def _manage_nav2(self, command, expected):
        if not self.lifecycle_manager_client.wait_for_service(timeout_sec=3):
            raise RuntimeError("Nav2 lifecycle manager 服务不可用")
        request = self.ManageLifecycleNodes.Request()
        request.command = command
        response = self._wait(self.lifecycle_manager_client.call_async(request), 20)
        if not response.success:
            raise RuntimeError("Nav2 lifecycle manager 拒绝状态变更")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = self.navigator_state()
            if state["label"] == expected:
                return state
            time.sleep(0.1)
        raise RuntimeError(f"Nav2 lifecycle manager 未使 bt_navigator 进入 {expected}")

    def snapshot(self, robot="robot_1"):
        self._require_robot(robot)
        nodes = [
            (namespace.rstrip("/") + "/" + name) or "/" + name
            for name, namespace in self.node.get_node_names_and_namespaces()
        ]
        return {
            "robot_id": robot,
            "nodes": sorted(set(nodes)),
            "topics": self.topic_health(robot),
            "pose": dict(self.ground_truth or self.pose or {}),
            "localization_pose": dict(self.pose) if self.pose else None,
            "nav2_lifecycle": {"bt_navigator": self.navigator_state(robot)},
            "observed": time.time(),
        }

    def parameters(self, robot="robot_1"):
        self._require_robot(robot)
        return {
            "scan_input": "/scan_faulted",
            "fault_boundary": "laser_forwarder",
            "controller": self.controller_config(robot),
            "nav2_lifecycle": {"bt_navigator": self.navigator_state(robot)},
            **self.topic_health(robot),
        }

    def sample(self, robot="robot_1"):
        self._require_robot(robot)
        with self.data_lock:
            before = dict(self.ground_truth or self.pose or {})
        time.sleep(0.7)
        with self.data_lock:
            after = dict(self.ground_truth or self.pose or {})
        return {
            "before": before,
            "after": after,
            "displacement": math.hypot(
                after.get("x", 0) - before.get("x", 0),
                after.get("y", 0) - before.get("y", 0),
            ),
        }

    def inject(self, robot, fault):
        self._require_robot(robot)
        if fault == "scan_interrupted":
            with self.data_lock:
                self.scan_forwarding = False
                self.scan_forwarded_times.clear()
        elif fault == "navigator_inactive":
            self.stop(robot)
            state = self.navigator_state(robot)
            if state["label"] == "active":
                self._manage_nav2(self.ManageLifecycleNodes.Request.PAUSE, "inactive")
        elif fault == "controller_velocity_zero":
            current = self.controller_config(robot)
            if current["vx_max"] is None:
                raise RuntimeError("无法读取 controller_server 参数")
            self.controller_defaults = current
            self._set_controller_config(0.0, 0.0)
        else:
            raise ValueError("不支持的 Nav2 故障类型")
        return {"applied": fault, "robot_id": robot}

    def set_scenario(self, robot, scenario):
        """Apply a fixed Gazebo scenario without exposing shell access to an Agent."""
        self._require_robot(robot)
        if scenario != "shelf_a_primary_blocked":
            raise ValueError("不支持的场景")
        request = (
            'name: "temporary_crate" '
            "position: {x: 2.0, y: 0.5, z: 0.35} orientation: {w: 1.0}"
        )
        command = [
            "gz", "service", "-s", "/world/rdl_warehouse/set_pose",
            "--reqtype", "gz.msgs.Pose", "--reptype", "gz.msgs.Boolean",
            "--timeout", "3000", "--req", request,
        ]
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0 or "true" not in result.stdout:
            raise RuntimeError("Gazebo 未能移动实体障碍箱")
        self.scenario = scenario
        # Give the real laser/costmap pipeline time to observe the moved entity.
        time.sleep(2.0)
        return {
            "applied": scenario,
            "robot_id": robot,
            "entity": "temporary_crate",
            "pose": {"x": 2.0, "y": 0.5, "z": 0.35},
        }

    def reset(self, robot):
        self._require_robot(robot)
        self.stop(robot)
        self._stop_sim()
        with self.data_lock:
            self.scan_forwarding = True
            self.scan_raw_times.clear()
            self.scan_forwarded_times.clear()
            self.trace.clear()
            self.pose = None
            self.ground_truth = None
        self._start_sim()
        self.scenario = None
        self.controller_defaults = self.controller_config()
        return {"reset": robot, "note": "Gazebo 世界和 Nav2 已恢复到初始位置"}

    def plan_path(self, robot, x, y, timeout=6.0):
        """Ask Nav2's actual planner whether a reviewed pose has a path."""
        self._require_robot(robot)
        if not self.path_client.wait_for_server(timeout_sec=3):
            return {
                "reachable": False, "target": [x, y],
                "reason": "compute_path_action_unavailable",
            }
        goal = self.ComputePathToPose.Goal()
        goal.goal.header.frame_id = "map"
        goal.goal.header.stamp = self.node.get_clock().now().to_msg()
        goal.goal.pose.position.x = float(x)
        goal.goal.pose.position.y = float(y)
        goal.goal.pose.orientation.w = 1.0
        goal.planner_id = "GridBased"
        goal.use_start = False
        try:
            handle = self._wait(self.path_client.send_goal_async(goal), 3)
            if not handle.accepted:
                return {
                    "reachable": False, "target": [x, y], "reason": "goal_rejected"
                }
            wrapped = self._wait(handle.get_result_async(), timeout)
            result = wrapped.result
            poses = list(result.path.poses)
            length = sum(
                math.hypot(
                    b.pose.position.x - a.pose.position.x,
                    b.pose.position.y - a.pose.position.y,
                )
                for a, b in zip(poses, poses[1:])
            )
            return {
                "reachable": int(result.error_code) == 0 and bool(poses),
                "target": [float(x), float(y)],
                "path_length": round(length, 3),
                "pose_count": len(poses),
                "error_code": int(result.error_code),
                "reason": result.error_msg or "path_found",
            }
        except (TimeoutError, RuntimeError):
            return {
                "reachable": False, "target": [x, y], "reason": "planning_timeout"
            }

    def navigate(self, robot, x=0.5, y=0.5, timeout=25.0, cancel=None):
        self._require_robot(robot)
        start = time.monotonic()
        goal = self.NavigateToPose.Goal()
        goal.pose.header.frame_id = "map"
        goal.pose.header.stamp = self.node.get_clock().now().to_msg()
        goal.pose.pose.position.x = float(x)
        goal.pose.pose.position.y = float(y)
        goal.pose.pose.orientation.w = 1.0
        try:
            handle = self._wait(self.client.send_goal_async(goal), 5)
        except (TimeoutError, RuntimeError):
            return self.verdict(robot, x, y, start, "navigate_action_unavailable")
        if not handle.accepted:
            return self.verdict(robot, x, y, start, "goal_rejected")
        self.active_goal = handle
        result_future = handle.get_result_async()
        try:
            deadline = time.monotonic() + timeout
            while not result_future.done() and time.monotonic() < deadline:
                if cancel and cancel.is_set():
                    self.stop(robot)
                    return self.verdict(robot, x, y, start, "canceled")
                time.sleep(0.1)
            if not result_future.done():
                self.stop(robot)
                return self.verdict(robot, x, y, start, "navigation_timeout")
            wrapped = result_future.result()
            message = getattr(wrapped.result, "error_msg", "") or str(wrapped.status)
            return self.verdict(robot, x, y, start, message)
        finally:
            self.active_goal = None

    def verdict(self, robot, x, y, start, message):
        self._require_robot(robot)
        with self.data_lock:
            pose = dict(self.ground_truth or self.pose or {})
        error = math.hypot(pose.get("x", 100) - x, pose.get("y", 100) - y)
        fresh = time.time() - pose.get("observed", 0) < 2
        return {
            "success": bool(fresh and error < 0.35),
            "error": round(error, 4),
            "elapsed": round(time.monotonic() - start, 2),
            "target": [x, y],
            "pose": pose,
            "message": message,
        }

    def repair(self, robot, cause):
        self._require_robot(robot)
        if cause == "scan_interrupted":
            with self.data_lock:
                self.scan_forwarding = True
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                health = self.topic_health(robot)
                if health["nav2_scan_hz"] > 0:
                    return {"operation": "restore_scan_forwarding", **health}
                time.sleep(0.1)
            return {"operation": "restore_scan_forwarding", **self.topic_health(robot)}
        if cause == "navigator_inactive":
            state = self.navigator_state(robot)
            if state["label"] == "inactive":
                state = self._manage_nav2(self.ManageLifecycleNodes.Request.RESUME, "active")
            if not self.client.wait_for_server(timeout_sec=3):
                raise RuntimeError("bt_navigator 已激活，但导航 action 尚未就绪")
            time.sleep(2.0)
            return {"operation": "resume_nav2_lifecycle", "state": state}
        if cause == "controller_velocity_zero":
            config = self._set_controller_config(
                self.controller_defaults["vx_max"], self.controller_defaults["vx_min"]
            )
            return {"operation": "restore_controller_velocity_limits", "controller": config}
        raise ValueError("No allowed repair for this diagnosis")

    def stop(self, robot):
        self._require_robot(robot)
        handle = self.active_goal
        if handle:
            try:
                self._wait(handle.cancel_goal_async(), 3)
            except Exception:
                pass

    def public(self):
        with self.data_lock:
            pose = dict(self.ground_truth or self.pose or {})
            trace = list(self.trace)
        return [
            {
                "id": "robot_1",
                "pose": pose,
                "trace": trace,
                "controller_alive": self.sim is not None and self.sim.poll() is None,
                "busy": self.locks["robot_1"].locked(),
                "scan": self.topic_health(),
                "runtime": "nav2_gazebo",
                "scenario_active": self.scenario is not None,
            }
        ]

    def close(self):
        self.stop("robot_1")
        self._stop_sim()
        self.executor.shutdown()
        self.node.destroy_node()
        rclpy.try_shutdown()
