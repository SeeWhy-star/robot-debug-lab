"""Bounded ROS operations. Fault labels are not exposed by observation tools."""

import concurrent.futures, math, os, signal, subprocess, threading, time
from collections import deque
import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rcl_interfaces.srv import GetParameters, SetParameters
from rcl_interfaces.msg import Parameter, ParameterValue, ParameterType
from geometry_msgs.msg import Twist
from turtlesim_msgs.msg import Pose
from turtlesim_msgs.srv import TeleportAbsolute
from rdl_interfaces.action import Navigate
from .config import ROOT, RUNTIME, ROBOTS, robot_id


class Bridge:
    def __init__(self):
        rclpy.init(signal_handler_options=rclpy.signals.SignalHandlerOptions.NO)
        self.node = Node("debug_lab_bridge")
        self.executor = MultiThreadedExecutor(num_threads=4)
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.thread.start()
        self.poses = {}
        self.traces = {r: deque(maxlen=250) for r in ROBOTS}
        self.commands = {}
        self.sims = {}
        self.controllers = {}
        self.handles = {}
        self.clients = {}
        self.logs = []
        self.locks = {r: threading.Lock() for r in ROBOTS}
        self.data_lock = threading.Lock()
        for r in ROBOTS:
            self.node.create_subscription(
                Pose, f"/{r}/turtle1/pose", lambda m, r=r: self._pose(r, m), 10
            )
            self.node.create_subscription(
                Twist, f"/{r}/turtle1/cmd_vel", lambda m, r=r: self._cmd(r, m), 10
            )
            self.clients[r] = ActionClient(self.node, Navigate, f"/{r}/navigate")
            self.start_sim(r)
            self.start_controller(r)
        deadline = time.monotonic() + 12
        while len(self.poses) < 2 and time.monotonic() < deadline:
            time.sleep(0.1)
        if len(self.poses) < 2:
            raise RuntimeError("TurtleSim startup failed; inspect .runtime/*.log")

    def _pose(self, r, m):
        with self.data_lock:
            self.poses[r] = dict(
                x=m.x,
                y=m.y,
                theta=m.theta,
                speed=m.linear_velocity,
                observed=time.time(),
            )
            if (
                not self.traces[r]
                or math.hypot(m.x - self.traces[r][-1][0], m.y - self.traces[r][-1][1])
                > 0.025
            ):
                self.traces[r].append([m.x, m.y])

    def _cmd(self, r, m):
        with self.data_lock:
            self.commands[r] = dict(
                linear=m.linear.x, angular=m.angular.z, observed=time.time()
            )

    def _spawn(self, label, cmd):
        f = (RUNTIME / f"{label}.log").open("a")
        self.logs.append(f)
        env = os.environ.copy()
        if env.get("RDL_NATIVE_GUI") != "1":
            env["QT_QPA_PLATFORM"] = "offscreen"
        env["RDL_PARENT_PID"] = str(os.getpid())
        return subprocess.Popen(
            [str(ROOT / ".venv/bin/python"), "-m", "robot_debug_lab.child", *cmd],
            cwd=ROOT,
            env=env,
            stdout=f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def _stop(self, p):
        if p and p.poll() is None:
            os.killpg(p.pid, signal.SIGINT)
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()

    def start_sim(self, r):
        robot_id(r)
        self.sims[r] = self._spawn(
            r + "-sim",
            [
                "/opt/ros/lyrical/lib/turtlesim/turtlesim_node",
                "--ros-args",
                "-r",
                f"__ns:=/{r}",
            ],
        )

    def start_controller(self, r, topic="turtle1/cmd_vel"):
        robot_id(r)
        self._stop(self.controllers.get(r))
        self.controllers[r] = self._spawn(
            r + "-controller",
            [
                str(ROOT / ".venv/bin/python"),
                "-m",
                "robot_debug_lab.controller",
                "--ros-args",
                "-r",
                f"__ns:=/{r}",
                "-p",
                f"command_topic:={topic}",
            ],
        )
        time.sleep(0.5)

    @staticmethod
    def wait(f, timeout=5):
        event = threading.Event()
        f.add_done_callback(lambda _: event.set())
        if not event.wait(timeout):
            raise TimeoutError("ROS operation timed out")
        if f.exception():
            raise f.exception()
        return f.result()

    def service(self, typ, path, request, timeout=5):
        c = self.node.create_client(typ, path)
        try:
            if not c.wait_for_service(timeout_sec=timeout):
                raise TimeoutError("Service unavailable: " + path)
            return self.wait(c.call_async(request), timeout)
        finally:
            self.node.destroy_client(c)

    def parameters(self, r):
        robot_id(r)
        q = GetParameters.Request()
        q.names = ["max_speed", "command_topic"]
        try:
            a = self.service(GetParameters, f"/{r}/controller/get_parameters", q, 2)
            return {
                "max_speed": a.values[0].double_value,
                "command_topic": a.values[1].string_value,
            }
        except TimeoutError:
            return {"available": False}

    def set_speed(self, r, value):
        robot_id(r)
        if not math.isfinite(value) or not 0 <= value <= 3:
            raise ValueError("Speed outside simulation limits")
        q = SetParameters.Request()
        q.parameters = [
            Parameter(
                name="max_speed",
                value=ParameterValue(
                    type=ParameterType.PARAMETER_DOUBLE, double_value=float(value)
                ),
            )
        ]
        a = self.service(SetParameters, f"/{r}/controller/set_parameters", q)
        if not a.results[0].successful:
            raise RuntimeError(a.results[0].reason)
        return {"max_speed": value}

    def stop(self, r):
        robot_id(r)
        h = self.handles.get(r)
        if h:
            try:
                self.wait(h.cancel_goal_async(), 2)
            except Exception:
                pass
        # Send zero velocity even if controller is missing or its output is misrouted.
        pub = self.node.create_publisher(Twist, f"/{r}/turtle1/cmd_vel", 10)
        try:
            for _ in range(3):
                pub.publish(Twist())
                time.sleep(0.05)
        finally:
            self.node.destroy_publisher(pub)

    def reset(self, r):
        self.stop(r)
        self.start_controller(r)
        q = TeleportAbsolute.Request()
        q.x = 5.54
        q.y = 5.54
        q.theta = 0.0
        self.service(TeleportAbsolute, f"/{r}/turtle1/teleport_absolute", q)
        with self.data_lock:
            self.traces[r].clear()
            self.commands.pop(r, None)
        return {"reset": r}

    def inject(self, r, fault):
        self.reset(r)
        if fault == "controller_stopped":
            self._stop(self.controllers[r])
        elif fault == "topic_mismatch":
            self.start_controller(r, "orphan_velocity")
        elif fault == "zero_speed":
            self.set_speed(r, 0.0)
        else:
            raise ValueError("Unknown fault")
        time.sleep(0.8)
        return {"applied": fault, "robot_id": r}

    def snapshot(self, r):
        robot_id(r)
        nodes = [
            ns.rstrip("/") + "/" + name
            for name, ns in self.node.get_node_names_and_namespaces()
            if ns == f"/{r}"
        ]
        pubs = []
        for topic, types in self.node.get_topic_names_and_types():
            if topic.startswith(f"/{r}/") and "geometry_msgs/msg/Twist" in types:
                pubs.append(
                    {
                        "topic": topic,
                        "publishers": [
                            i.node_name
                            for i in self.node.get_publishers_info_by_topic(topic)
                        ],
                        "subscribers": [
                            i.node_name
                            for i in self.node.get_subscriptions_info_by_topic(topic)
                        ],
                    }
                )
        with self.data_lock:
            return dict(
                robot_id=r,
                nodes=nodes,
                velocity_topics=pubs,
                pose=self.poses.get(r),
                last_command=self.commands.get(r),
                observed=time.time(),
            )

    def sample(self, r):
        robot_id(r)
        with self.data_lock:
            a = dict(self.poses.get(r, {}))
        time.sleep(0.7)
        with self.data_lock:
            b = dict(self.poses.get(r, {}))
        return {
            "before": a,
            "after": b,
            "displacement": math.hypot(
                b.get("x", 0) - a.get("x", 0), b.get("y", 0) - a.get("y", 0)
            ),
        }

    def navigate(self, r, x=8.0, y=8.0, timeout=7.0, cancel=None):
        robot_id(r)
        c = self.clients[r]
        start = time.monotonic()
        if not c.wait_for_server(timeout_sec=2):
            return self.verdict(r, x, y, start, "action_unavailable")
        g = Navigate.Goal()
        g.x = float(x)
        g.y = float(y)
        g.timeout_seconds = float(timeout)
        h = self.wait(c.send_goal_async(g), 3)
        if not h.accepted:
            return self.verdict(r, x, y, start, "goal_rejected")
        self.handles[r] = h
        future = h.get_result_async()
        try:
            until = time.monotonic() + timeout + 3
            while not future.done() and time.monotonic() < until:
                if cancel and cancel.is_set():
                    self.stop(r)
                    return self.verdict(r, x, y, start, "canceled")
                time.sleep(0.1)
            if not future.done():
                self.stop(r)
                return self.verdict(r, x, y, start, "transport_timeout")
            result = future.result()
            return self.verdict(r, x, y, start, result.result.message)
        finally:
            self.handles.pop(r, None)

    def verdict(self, r, x, y, start, message):
        # Independent of controller success flag and model output.
        with self.data_lock:
            p = dict(self.poses.get(r, {}))
        error = math.hypot(p.get("x", 100) - x, p.get("y", 100) - y)
        fresh = time.time() - p.get("observed", 0) < 1
        return dict(
            success=bool(
                fresh
                and error < 0.22
                and message
                not in [
                    "canceled",
                    "goal_rejected",
                    "action_unavailable",
                    "transport_timeout",
                ]
            ),
            error=round(error, 4),
            elapsed=round(time.monotonic() - start, 2),
            target=[x, y],
            pose=p,
            message=message,
        )

    def repair(self, r, cause):
        if cause == "controller_stopped":
            self.start_controller(r)
        elif cause == "topic_mismatch":
            self.start_controller(r)
        elif cause == "zero_speed":
            self.set_speed(r, 2.0)
        else:
            raise ValueError("No allowed repair for this diagnosis")
        return {"operation": cause, "parameters": self.parameters(r)}

    def public(self):
        with self.data_lock:
            return [
                dict(
                    id=r,
                    pose=self.poses.get(r),
                    trace=list(self.traces[r]),
                    controller_alive=self.controllers[r].poll() is None,
                    busy=self.locks[r].locked(),
                )
                for r in ROBOTS
            ]

    def close(self):
        for r in ROBOTS:
            self.stop(r)
        for p in list(self.controllers.values()) + list(self.sims.values()):
            self._stop(p)
        self.executor.shutdown()
        self.node.destroy_node()
        rclpy.try_shutdown()
        for f in self.logs:
            f.close()
