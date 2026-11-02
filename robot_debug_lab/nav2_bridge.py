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
from types import SimpleNamespace

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

from .config import ROOT, RUNTIME, ROBOTS, ROBOT_STARTS


WAREHOUSE_WORLD = ROOT / "simulation" / "worlds" / "rdl_warehouse.sdf.xacro"
WAREHOUSE_MAP = ROOT / "simulation" / "maps" / "rdl_warehouse.yaml"
START_POSES = ROBOT_STARTS
LOCALIZATION_DRIFT_THRESHOLD = 0.45


def rewrite_nav2_params(text):
    """Guard navigation laser consumers while keeping AMCL localization live."""
    text = re.sub(
        r'^(\s*topic:\s*)[\"\']?scan[\"\']?\s*$',
        r"\1scan_faulted",
        text,
        flags=re.MULTILINE,
    )
    # Each AMCL instance otherwise races while renaming the same default
    # /tmp/amcl_saved_pose file. This lab explicitly seeds every start/reset.
    text = re.sub(
        r"^(\s*save_pose_rate:\s*)[0-9.]+\s*$",
        r"\g<1>0.0",
        text,
        flags=re.MULTILINE,
    )
    # Stateful goal checking can remember that XY was reached while the robot
    # rotates, even after localization moves the physical pose outside the
    # tolerance. Keep checking XY and use a tighter Nav2 tolerance than the
    # independent 0.35 m Gazebo acceptance boundary.
    text = re.sub(
        r"(general_goal_checker:\s*\n\s*)stateful:\s*true",
        r"\1stateful: false",
        text,
    )
    return re.sub(
        r"(general_goal_checker:[\s\S]*?xy_goal_tolerance:\s*)[0-9.]+",
        r"\g<1>0.15",
        text,
        count=1,
    )


class Nav2Bridge:
    robot_ids = ROBOTS

    def __init__(self):
        try:
            from nav2_msgs.action import ComputePathToPose, NavigateToPose
            from nav2_msgs.srv import ClearEntireCostmap, ManageLifecycleNodes
        except ImportError as error:
            raise RuntimeError(
                "缺少 Nav2/Gazebo。请运行 bash scripts/install_nav2_sim.sh"
            ) from error

        self.NavigateToPose = NavigateToPose
        self.ComputePathToPose = ComputePathToPose
        self.ClearEntireCostmap = ClearEntireCostmap
        self.ManageLifecycleNodes = ManageLifecycleNodes
        rclpy.init(signal_handler_options=rclpy.signals.SignalHandlerOptions.NO)
        self.node = Node(
            "robot_debug_nav2_bridge",
            parameter_overrides=[Parameter("use_sim_time", value=True)],
        )
        self.executor = MultiThreadedExecutor(num_threads=8)
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.thread.start()

        self.data_lock = threading.RLock()
        self.locks = {robot: threading.Lock() for robot in self.robot_ids}
        self.robots = {}
        self.scenario = None
        self.log_handle = None
        self.sim = None
        self.gui = None
        initial_pose_qos = QoSProfile(
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        for robot in self.robot_ids:
            prefix = f"/{robot}"
            runtime = SimpleNamespace(
                pose=None,
                ground_truth=None,
                trace=deque(maxlen=500),
                scan_forwarding=True,
                scan_raw_times=deque(maxlen=200),
                scan_forwarded_times=deque(maxlen=200),
                active_goal=None,
                navigation_attempt=0,
                scheduled_fault=None,
                controller_defaults={"vx_max": 0.5, "vx_min": -0.35},
            )
            runtime.scan_pub = self.node.create_publisher(
                LaserScan, f"{prefix}/scan_faulted", qos_profile_sensor_data
            )
            runtime.initial_pose_pub = self.node.create_publisher(
                PoseWithCovarianceStamped, f"{prefix}/initialpose", initial_pose_qos
            )
            runtime.client = ActionClient(
                self.node, NavigateToPose, f"{prefix}/navigate_to_pose"
            )
            runtime.path_client = ActionClient(
                self.node, ComputePathToPose, f"{prefix}/compute_path_to_pose"
            )
            runtime.lifecycle_client = self.node.create_client(
                GetState, f"{prefix}/bt_navigator/get_state"
            )
            runtime.amcl_lifecycle_client = self.node.create_client(
                GetState, f"{prefix}/amcl/get_state"
            )
            runtime.lifecycle_change_client = self.node.create_client(
                ChangeState, f"{prefix}/bt_navigator/change_state"
            )
            runtime.controller_get_client = self.node.create_client(
                GetParameters, f"{prefix}/controller_server/get_parameters"
            )
            runtime.controller_set_client = self.node.create_client(
                SetParameters, f"{prefix}/controller_server/set_parameters"
            )
            runtime.lifecycle_manager_client = self.node.create_client(
                ManageLifecycleNodes, f"{prefix}/lifecycle_manager_nav2/manage_nodes"
            )
            runtime.local_costmap_clear_client = self.node.create_client(
                ClearEntireCostmap,
                f"{prefix}/local_costmap/clear_entirely_local_costmap",
            )
            runtime.global_costmap_clear_client = self.node.create_client(
                ClearEntireCostmap,
                f"{prefix}/global_costmap/clear_entirely_global_costmap",
            )
            self.robots[robot] = runtime
            self.node.create_subscription(
                LaserScan, f"{prefix}/scan",
                lambda message, rid=robot: self._scan(rid, message),
                qos_profile_sensor_data,
            )
            self.node.create_subscription(
                PoseWithCovarianceStamped, f"{prefix}/amcl_pose",
                lambda message, rid=robot: self._pose(rid, message), 10,
            )
            self.node.create_subscription(
                Odometry, f"{prefix}/odom",
                lambda message, rid=robot: self._odom(rid, message), 10,
            )

        self.params = self._prepare_nav2_params()
        try:
            self._start_sim()
            for robot in self.robot_ids:
                self.robots[robot].controller_defaults = self.controller_config(robot)
        except Exception:
            self._stop_sim()
            raise

    def _require_robot(self, robot):
        if robot not in self.robots:
            raise ValueError("当前仿真中不存在该机器人")

    def _rt(self, robot):
        self._require_robot(robot)
        return self.robots[robot]

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
        command = [
            "/usr/bin/python3",
            str(ROOT / "simulation/launch/rdl_multi_robot.launch.py"),
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
        for robot in self.robot_ids:
            if not self._rt(robot).client.wait_for_server(timeout_sec=60):
                if self.sim.poll() is not None:
                    raise RuntimeError(
                        "Nav2/Gazebo 启动失败，请查看 .runtime/nav2-simulation.log"
                    )
                raise RuntimeError(f"{robot} Nav2 导航服务在 60 秒内未就绪")
        # The action server can be discoverable while AMCL is still inactive,
        # but the rest of Nav2 cannot activate until AMCL publishes map->odom.
        # Seed after AMCL itself is active, then wait for the whole stack.
        for robot in self.robot_ids:
            self._wait_amcl_active(robot)
        for robot in self.robot_ids:
            self._set_initial_pose(robot)
        for robot in self.robot_ids:
            self._wait_nav2_active(robot)

    def _refresh_action_clients(self):
        """Discard DDS endpoints from the previous Nav2 process.

        A client object can briefly keep the old action server discovery state
        after a rapid shared-world reset.  Reusing it may let readiness checks
        pass while the first goal is still sent to a dead endpoint.  Recreate
        both action clients while Nav2 is stopped so the next launch performs
        fresh discovery.
        """
        for robot in self.robot_ids:
            runtime = self._rt(robot)
            runtime.client.destroy()
            runtime.path_client.destroy()
            prefix = f"/{robot}"
            runtime.client = ActionClient(
                self.node, self.NavigateToPose, f"{prefix}/navigate_to_pose"
            )
            runtime.path_client = ActionClient(
                self.node, self.ComputePathToPose,
                f"{prefix}/compute_path_to_pose",
            )

    def _wait_action_servers_gone(self, timeout=25.0):
        """Wait for DDS to remove the stopped Nav2 action endpoints."""
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            if all(
                not runtime.client.server_is_ready()
                and not runtime.path_client.server_is_ready()
                for runtime in self.robots.values()
            ):
                return True
            time.sleep(0.1)
        return False

    def _prepare_sim_restart(self):
        self._stop_sim()
        # Readiness on the old ActionClient can remain cached after its server
        # participant is gone.  Recreate first, then ask the fresh clients
        # whether DDS still exposes any endpoint from the stopped process.
        self._refresh_action_clients()
        if not self._wait_action_servers_gone():
            raise RuntimeError(
                "旧Nav2 action端点未从ROS图清除，拒绝在不确定状态下重启"
            )

    def _wait_amcl_active(self, robot, timeout=45):
        rt = self._rt(robot)
        if not rt.amcl_lifecycle_client.wait_for_service(timeout_sec=timeout):
            raise RuntimeError(f"{robot} AMCL 生命周期服务未就绪")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                state = self._wait(
                    rt.amcl_lifecycle_client.call_async(GetState.Request()), timeout=3
                )
                if state.current_state.label == "active":
                    return
            except TimeoutError:
                pass
            time.sleep(0.3)
        raise RuntimeError(f"{robot} AMCL 在限定时间内未进入 active 状态")

    def _wait_nav2_active(self, robot, timeout=45):
        """Wait for the action server to be usable, not merely discoverable."""
        rt = self._rt(robot)
        if not rt.lifecycle_client.wait_for_service(timeout_sec=timeout):
            raise RuntimeError(f"{robot} Nav2 生命周期服务未就绪")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.sim and self.sim.poll() is not None:
                raise RuntimeError(
                    "Nav2/Gazebo 启动失败，请查看 .runtime/nav2-simulation.log"
                )
            try:
                state = self._wait(
                    rt.lifecycle_client.call_async(GetState.Request()), timeout=3
                )
                if state.current_state.label == "active":
                    return
            except TimeoutError:
                pass
            time.sleep(0.5)
        raise RuntimeError("Nav2 在限定时间内未进入 active 状态")

    def _stop_sim(self):
        if self.gui and self.gui.poll() is None:
            os.killpg(self.gui.pid, signal.SIGINT)
            try:
                self.gui.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(self.gui.pid, signal.SIGKILL)
                self.gui.wait()
        self.gui = None
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

    def _set_initial_pose(self, robot):
        rt = self._rt(robot)
        start_x, start_y, start_yaw = START_POSES[robot]
        message = PoseWithCovarianceStamped()
        message.header.frame_id = "map"
        message.pose.pose.position.x = start_x
        message.pose.pose.position.y = start_y
        message.pose.pose.orientation.z = math.sin(start_yaw / 2.0)
        message.pose.pose.orientation.w = math.cos(start_yaw / 2.0)
        message.pose.covariance[0] = 0.25
        message.pose.covariance[7] = 0.25
        message.pose.covariance[35] = 0.07
        deadline = time.monotonic() + 20
        while rt.pose is None and time.monotonic() < deadline:
            # A continuous 2 Hz reset can starve AMCL before its next scan
            # update. Publish a short reliable burst, then leave a convergence
            # window before deciding whether another burst is needed.
            for _ in range(3):
                message.header.stamp = self.node.get_clock().now().to_msg()
                rt.initial_pose_pub.publish(message)
                time.sleep(0.15)
            settle_until = min(deadline, time.monotonic() + 1.5)
            while rt.pose is None and time.monotonic() < settle_until:
                time.sleep(0.1)
        if rt.pose is None:
            raise RuntimeError(
                f"{robot} Nav2 已启动但定位未就绪，请查看 .runtime/nav2-simulation.log"
            )

    def _publish_initial_pose(self, robot, x, y, yaw=0.0, variance=0.0025, repeats=6):
        """Publish one bounded AMCL seed through the real ROS interface."""
        rt = self._rt(robot)
        message = PoseWithCovarianceStamped()
        message.header.frame_id = "map"
        message.pose.pose.position.x = float(x)
        message.pose.pose.position.y = float(y)
        message.pose.pose.orientation.z = math.sin(float(yaw) / 2.0)
        message.pose.pose.orientation.w = math.cos(float(yaw) / 2.0)
        message.pose.covariance[0] = float(variance)
        message.pose.covariance[7] = float(variance)
        message.pose.covariance[35] = max(float(variance), 0.001)
        for _ in range(repeats):
            message.header.stamp = self.node.get_clock().now().to_msg()
            rt.initial_pose_pub.publish(message)
            time.sleep(0.15)

    def localization_health(self, robot="robot_1"):
        rt = self._rt(robot)
        with self.data_lock:
            truth = dict(rt.ground_truth) if rt.ground_truth else None
            estimate = dict(rt.pose) if rt.pose else None
        if not truth or not estimate:
            return {
                "status": "unavailable",
                "position_error": None,
                "gazebo_pose": truth,
                "amcl_pose": estimate,
                "observed": time.time(),
            }
        position_error = math.hypot(
            truth["x"] - estimate["x"], truth["y"] - estimate["y"]
        )
        return {
            "status": (
                "drifted"
                if position_error >= LOCALIZATION_DRIFT_THRESHOLD
                else "aligned"
            ),
            "position_error": round(position_error, 4),
            "threshold": LOCALIZATION_DRIFT_THRESHOLD,
            "gazebo_pose": truth,
            "amcl_pose": estimate,
            "observed": time.time(),
        }

    def _wait_localization_near(self, robot, x, y, timeout=6.0):
        rt = self._rt(robot)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.data_lock:
                estimate = dict(rt.pose) if rt.pose else None
            if estimate and math.hypot(estimate["x"] - x, estimate["y"] - y) < 0.3:
                return estimate
            time.sleep(0.1)
        raise RuntimeError("AMCL 未接受经过审核的重定位位姿")

    def _scan(self, robot, message):
        rt = self._rt(robot)
        now = time.time()
        with self.data_lock:
            rt.scan_raw_times.append(now)
            forwarding = rt.scan_forwarding
            if forwarding:
                rt.scan_forwarded_times.append(now)
        if forwarding:
            rt.scan_pub.publish(message)

    def _pose(self, robot, message):
        rt = self._rt(robot)
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
            rt.pose = current

    def _odom(self, robot, message):
        """Convert simulator odometry into map coordinates for independent QA."""
        rt = self._rt(robot)
        start_x, start_y, _ = START_POSES[robot]
        p = message.pose.pose
        z, w = p.orientation.z, p.orientation.w
        current = {
            "x": p.position.x + start_x,
            "y": p.position.y + start_y,
            "theta": 2.0 * math.atan2(z, w),
            "speed": math.hypot(
                message.twist.twist.linear.x, message.twist.twist.linear.y
            ),
            "observed": time.time(),
            "source": "gazebo_odometry",
        }
        with self.data_lock:
            rt.ground_truth = current
            if (
                not rt.trace
                or math.hypot(
                    current["x"] - rt.trace[-1][0],
                    current["y"] - rt.trace[-1][1],
                )
                > 0.04
            ):
                rt.trace.append([current["x"], current["y"]])

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
        rt = self._rt(robot)
        with self.data_lock:
            return {
                "raw_scan_hz": self._rate(tuple(rt.scan_raw_times)),
                "nav2_scan_hz": self._rate(tuple(rt.scan_forwarded_times)),
                "scan_forwarding": rt.scan_forwarding,
                "observed": time.time(),
            }

    def navigator_state(self, robot="robot_1"):
        rt = self._rt(robot)
        node_name = f"/{robot}/bt_navigator"
        if not rt.lifecycle_client.wait_for_service(timeout_sec=2):
            return {"node": node_name, "id": 0, "label": "unavailable"}
        response = self._wait(rt.lifecycle_client.call_async(GetState.Request()), 3)
        return {
            "node": node_name,
            "id": int(response.current_state.id),
            "label": response.current_state.label,
        }

    def controller_config(self, robot="robot_1"):
        rt = self._rt(robot)
        if not rt.controller_get_client.wait_for_service(timeout_sec=2):
            return {"vx_max": None, "vx_min": None}
        request = GetParameters.Request()
        request.names = ["FollowPath.vx_max", "FollowPath.vx_min"]
        response = self._wait(rt.controller_get_client.call_async(request), 3)
        values = [value.double_value for value in response.values]
        return {"vx_max": values[0], "vx_min": values[1]}

    def _set_controller_config(self, robot, vx_max, vx_min):
        rt = self._rt(robot)
        if not rt.controller_set_client.wait_for_service(timeout_sec=2):
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
        response = self._wait(rt.controller_set_client.call_async(request), 4)
        if len(response.results) != 2 or not all(result.successful for result in response.results):
            reasons = [result.reason for result in response.results]
            raise RuntimeError("controller_server 拒绝参数变更：" + "; ".join(reasons))
        return self.controller_config(robot)

    def _change_navigator(self, robot, transition_id, expected):
        rt = self._rt(robot)
        if not rt.lifecycle_change_client.wait_for_service(timeout_sec=2):
            raise RuntimeError("bt_navigator lifecycle 服务不可用")
        request = ChangeState.Request()
        request.transition = Transition(id=transition_id)
        response = self._wait(rt.lifecycle_change_client.call_async(request), 5)
        if not response.success:
            raise RuntimeError("bt_navigator lifecycle 状态变更被拒绝")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.navigator_state(robot)
            if state["label"] == expected:
                return state
            time.sleep(0.1)
        raise RuntimeError(f"bt_navigator 未进入 {expected} 状态")

    def _manage_nav2(self, robot, command, expected):
        rt = self._rt(robot)
        if not rt.lifecycle_manager_client.wait_for_service(timeout_sec=3):
            raise RuntimeError("Nav2 lifecycle manager 服务不可用")
        request = self.ManageLifecycleNodes.Request()
        request.command = command
        response = self._wait(rt.lifecycle_manager_client.call_async(request), 20)
        if not response.success:
            raise RuntimeError("Nav2 lifecycle manager 拒绝状态变更")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = self.navigator_state(robot)
            if state["label"] == expected:
                return state
            time.sleep(0.1)
        raise RuntimeError(f"Nav2 lifecycle manager 未使 bt_navigator 进入 {expected}")

    def snapshot(self, robot="robot_1"):
        rt = self._rt(robot)
        nodes = [
            (namespace.rstrip("/") + "/" + name) or "/" + name
            for name, namespace in self.node.get_node_names_and_namespaces()
        ]
        nodes = [
            name for name in nodes
            if name.startswith(f"/{robot}/") or name == "/robot_debug_nav2_bridge"
        ]
        return {
            "robot_id": robot,
            "nodes": sorted(set(nodes)),
            "topics": self.topic_health(robot),
            "pose": dict(rt.ground_truth or rt.pose or {}),
            "localization_pose": dict(rt.pose) if rt.pose else None,
            "localization_health": self.localization_health(robot),
            "nav2_lifecycle": {"bt_navigator": self.navigator_state(robot)},
            "observed": time.time(),
        }

    def parameters(self, robot="robot_1"):
        self._require_robot(robot)
        return {
            "scan_input": f"/{robot}/scan_faulted",
            "fault_boundary": "laser_forwarder",
            "controller": self.controller_config(robot),
            "nav2_lifecycle": {"bt_navigator": self.navigator_state(robot)},
            **self.topic_health(robot),
        }

    def sample(self, robot="robot_1"):
        rt = self._rt(robot)
        with self.data_lock:
            before = dict(rt.ground_truth or rt.pose or {})
        time.sleep(0.7)
        with self.data_lock:
            after = dict(rt.ground_truth or rt.pose or {})
        return {
            "before": before,
            "after": after,
            "displacement": math.hypot(
                after.get("x", 0) - before.get("x", 0),
                after.get("y", 0) - before.get("y", 0),
            ),
        }

    def inject(self, robot, fault):
        rt = self._rt(robot)
        if fault == "scan_interrupted":
            with self.data_lock:
                rt.scan_forwarding = False
                rt.scan_forwarded_times.clear()
        elif fault == "navigator_inactive":
            self.stop(robot)
            state = self.navigator_state(robot)
            if state["label"] == "active":
                self._manage_nav2(
                    robot, self.ManageLifecycleNodes.Request.PAUSE, "inactive"
                )
        elif fault == "controller_velocity_zero":
            current = self.controller_config(robot)
            if current["vx_max"] is None:
                raise RuntimeError("无法读取 controller_server 参数")
            rt.controller_defaults = current
            self._set_controller_config(robot, 0.0, 0.0)
        elif fault == "localization_drift":
            with self.data_lock:
                truth = dict(rt.ground_truth) if rt.ground_truth else None
            if not truth:
                raise RuntimeError("Gazebo 真值尚未就绪，无法注入定位漂移")
            injected = {
                "x": truth["x"] + 1.5,
                "y": truth["y"],
                "theta": truth.get("theta", 0.0),
            }
            self._publish_initial_pose(
                robot, injected["x"], injected["y"], injected["theta"]
            )
            self._wait_localization_near(robot, injected["x"], injected["y"])
        else:
            raise ValueError("不支持的 Nav2 故障类型")
        return {"applied": fault, "robot_id": robot}

    def schedule_fault(self, robot, fault, navigation_attempt):
        """Arm one real fault for a future navigation call.

        The schedule belongs to the deterministic experiment harness.  It is
        intentionally absent from snapshot()/parameters(), so diagnostic
        agents can only observe the ROS/Nav2 state produced when it fires.
        """
        rt = self._rt(robot)
        if fault not in {
            "scan_interrupted", "navigator_inactive", "controller_velocity_zero",
            "localization_drift",
        }:
            raise ValueError("不支持的 Nav2 故障类型")
        if not isinstance(navigation_attempt, int) or not 1 <= navigation_attempt <= 8:
            raise ValueError("导航触发序号必须在 1 到 8 之间")
        with self.data_lock:
            rt.navigation_attempt = 0
            rt.scheduled_fault = {
                "kind": fault, "navigation_attempt": navigation_attempt,
            }
        return {
            "armed": True,
            "robot_id": robot,
            "navigation_attempt": navigation_attempt,
        }

    def _fire_scheduled_fault(self, robot):
        rt = self._rt(robot)
        with self.data_lock:
            rt.navigation_attempt += 1
            schedule = rt.scheduled_fault
            if not schedule or rt.navigation_attempt != schedule["navigation_attempt"]:
                return
            rt.scheduled_fault = None
            fault = schedule["kind"]
        self.inject(robot, fault)

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
        for rid in self.robot_ids:
            self.stop(rid)
        self._prepare_sim_restart()
        with self.data_lock:
            for runtime in self.robots.values():
                runtime.scan_forwarding = True
                runtime.scan_raw_times.clear()
                runtime.scan_forwarded_times.clear()
                runtime.trace.clear()
                runtime.pose = None
                runtime.ground_truth = None
                runtime.navigation_attempt = 0
                runtime.scheduled_fault = None
        startup_attempts = 1
        try:
            self._start_sim()
        except RuntimeError:
            # A failed ROS launch cannot be reused safely. One full retry is
            # bounded, visible to the caller, and avoids leaving a half-live
            # shared world behind after an intermittent discovery race.
            self._prepare_sim_restart()
            with self.data_lock:
                for runtime in self.robots.values():
                    runtime.pose = None
                    runtime.ground_truth = None
                    runtime.scan_raw_times.clear()
                    runtime.scan_forwarded_times.clear()
            startup_attempts = 2
            self._start_sim()
        self.scenario = None
        for rid in self.robot_ids:
            self.robots[rid].controller_defaults = self.controller_config(rid)
        return {
            "reset": "fleet",
            "requested_by": robot,
            "robots": list(self.robot_ids),
            "startup_attempts": startup_attempts,
            "note": "共享Gazebo世界和两套Nav2已恢复到各自初始位置",
        }

    @staticmethod
    def _sample_path(poses, spacing=0.20):
        points = []
        for stamped in poses:
            point = [
                round(float(stamped.pose.position.x), 3),
                round(float(stamped.pose.position.y), 3),
            ]
            if not points or math.hypot(
                point[0] - points[-1][0], point[1] - points[-1][1]
            ) >= spacing:
                points.append(point)
        if poses:
            final = [
                round(float(poses[-1].pose.position.x), 3),
                round(float(poses[-1].pose.position.y), 3),
            ]
            if not points or final != points[-1]:
                points.append(final)
        return points

    def plan_path(self, robot, x, y, timeout=6.0, include_path=False):
        """Ask Nav2's actual planner whether a reviewed pose has a path."""
        rt = self._rt(robot)
        if not rt.path_client.wait_for_server(timeout_sec=3):
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
            handle = self._wait(rt.path_client.send_goal_async(goal), 3)
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
            response = {
                "reachable": int(result.error_code) == 0 and bool(poses),
                "target": [float(x), float(y)],
                "path_length": round(length, 3),
                "pose_count": len(poses),
                "error_code": int(result.error_code),
                "reason": result.error_msg or "path_found",
            }
            if include_path:
                response["path"] = self._sample_path(poses)
            return response
        except (TimeoutError, RuntimeError):
            return {
                "reachable": False, "target": [x, y], "reason": "planning_timeout"
            }

    def navigate(self, robot, x=0.5, y=0.5, timeout=25.0, cancel=None):
        rt = self._rt(robot)
        self._fire_scheduled_fault(robot)
        start = time.monotonic()
        goal = self.NavigateToPose.Goal()
        goal.pose.header.frame_id = "map"
        goal.pose.header.stamp = self.node.get_clock().now().to_msg()
        goal.pose.pose.position.x = float(x)
        goal.pose.pose.position.y = float(y)
        goal.pose.pose.orientation.w = 1.0
        try:
            handle = self._wait(rt.client.send_goal_async(goal), 5)
        except (TimeoutError, RuntimeError):
            return self.verdict(
                robot, x, y, start, "navigate_action_unavailable", nav2_success=False
            )
        if not handle.accepted:
            return self.verdict(robot, x, y, start, "goal_rejected", nav2_success=False)
        rt.active_goal = handle
        result_future = handle.get_result_async()
        try:
            deadline = time.monotonic() + timeout
            while not result_future.done() and time.monotonic() < deadline:
                if cancel and cancel.is_set():
                    self.stop(robot)
                    return self.verdict(robot, x, y, start, "canceled", nav2_success=False)
                time.sleep(0.1)
            if not result_future.done():
                self.stop(robot)
                return self.verdict(
                    robot, x, y, start, "navigation_timeout", nav2_success=False
                )
            wrapped = result_future.result()
            message = getattr(wrapped.result, "error_msg", "") or str(wrapped.status)
            return self.verdict(
                robot, x, y, start, message, nav2_success=int(wrapped.status) == 4
            )
        finally:
            rt.active_goal = None

    def verdict(self, robot, x, y, start, message, nav2_success=None):
        rt = self._rt(robot)
        with self.data_lock:
            pose = dict(rt.ground_truth or rt.pose or {})
        error = math.hypot(pose.get("x", 100) - x, pose.get("y", 100) - y)
        fresh = time.time() - pose.get("observed", 0) < 2
        return {
            "success": bool(fresh and error < 0.35),
            "nav2_success": (
                bool(fresh and error < 0.35)
                if nav2_success is None else bool(nav2_success)
            ),
            "error": round(error, 4),
            "elapsed": round(time.monotonic() - start, 2),
            "target": [x, y],
            "pose": pose,
            "message": message,
        }

    def _clear_nav2_costmaps(self, robot):
        rt = self._rt(robot)
        cleared = {}
        for name, client in {
            "local": rt.local_costmap_clear_client,
            "global": rt.global_costmap_clear_client,
        }.items():
            if not client.wait_for_service(timeout_sec=2):
                raise RuntimeError(f"{name} costmap 清理服务不可用")
            request = self.ClearEntireCostmap.Request()
            response = self._wait(client.call_async(request), 3)
            if not response.success:
                raise RuntimeError(f"{name} costmap 清理失败")
            cleared[name] = True
        return cleared

    def repair(self, robot, cause):
        rt = self._rt(robot)
        if cause == "scan_interrupted":
            with self.data_lock:
                rt.scan_forwarding = True
            # One forwarded packet only proves that the switch flipped.  Wait
            # for a sustained stream, then clear obstacle layers built while
            # the sensor was stale before replaying the original goal.
            deadline = time.monotonic() + 5
            health = self.topic_health(robot)
            while time.monotonic() < deadline:
                health = self.topic_health(robot)
                if health["nav2_scan_hz"] >= max(
                    1.0, min(3.0, health["raw_scan_hz"] * 0.6)
                ):
                    break
                time.sleep(0.1)
            if health["nav2_scan_hz"] < 1.0:
                raise RuntimeError("激光转发未恢复为稳定数据流")
            cleared = self._clear_nav2_costmaps(robot)
            time.sleep(0.5)
            return {
                "operation": "restore_scan_forwarding",
                **self.topic_health(robot),
                "stale_costmaps_cleared": cleared,
            }
        if cause == "navigator_inactive":
            state = self.navigator_state(robot)
            if state["label"] == "inactive":
                state = self._manage_nav2(
                    robot, self.ManageLifecycleNodes.Request.RESUME, "active"
                )
            if not rt.client.wait_for_server(timeout_sec=3):
                raise RuntimeError("bt_navigator 已激活，但导航 action 尚未就绪")
            time.sleep(2.0)
            return {"operation": "resume_nav2_lifecycle", "state": state}
        if cause == "controller_velocity_zero":
            config = self._set_controller_config(
                robot, rt.controller_defaults["vx_max"], rt.controller_defaults["vx_min"]
            )
            return {"operation": "restore_controller_velocity_limits", "controller": config}
        if cause == "localization_drift":
            with self.data_lock:
                truth = dict(rt.ground_truth) if rt.ground_truth else None
            if not truth:
                raise RuntimeError("Gazebo 真值不可用，拒绝重定位")
            self._publish_initial_pose(
                robot, truth["x"], truth["y"], truth.get("theta", 0.0), variance=0.04
            )
            self._wait_localization_near(robot, truth["x"], truth["y"])
            # The AMCL callback can update before map->odom reaches every Nav2
            # consumer. Give TF one bounded propagation window, clear costmaps
            # in the new frame, then re-check against fresh Gazebo truth.
            time.sleep(1.0)
            cleared = self._clear_nav2_costmaps(robot)
            time.sleep(0.5)
            health = self.localization_health(robot)
            if health["status"] != "aligned":
                raise RuntimeError("AMCL 重定位后仍未与 Gazebo 真值对齐")
            return {
                "operation": "reseed_amcl_from_gazebo_truth",
                "localization": health,
                "stale_costmaps_cleared": cleared,
            }
        raise ValueError("No allowed repair for this diagnosis")

    def stop(self, robot):
        rt = self._rt(robot)
        handle = rt.active_goal
        if handle:
            try:
                self._wait(handle.cancel_goal_async(), 3)
            except Exception:
                pass

    def public(self):
        result = []
        for robot in self.robot_ids:
            rt = self._rt(robot)
            with self.data_lock:
                pose = dict(rt.ground_truth or rt.pose or {})
                trace = list(rt.trace)
            result.append({
                "id": robot,
                "pose": pose,
                "trace": trace,
                "controller_alive": (
                    self.sim is not None
                    and self.sim.poll() is None
                    and rt.client.server_is_ready()
                    and rt.lifecycle_client.service_is_ready()
                ),
                "nav2_online": (
                    rt.client.server_is_ready()
                    and rt.lifecycle_client.service_is_ready()
                ),
                "busy": self.locks[robot].locked(),
                "scan": self.topic_health(robot),
                "localization": self.localization_health(robot),
                "runtime": "nav2_gazebo",
                "scenario_active": self.scenario is not None,
            })
        return result

    def close(self):
        for robot in self.robot_ids:
            self.stop(robot)
        self._stop_sim()
        self.executor.shutdown()
        self.node.destroy_node()
        rclpy.try_shutdown()
