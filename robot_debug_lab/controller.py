"""Actual ROS 2 goal controller, separate from any diagnostic agent."""

import math
import threading
import time
import rclpy
from rclpy.node import Node
from rclpy.action import ActionServer, GoalResponse, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from geometry_msgs.msg import Twist
from turtlesim_msgs.msg import Pose
from rdl_interfaces.action import Navigate


class Controller(Node):
    def __init__(self):
        super().__init__("controller")
        self.declare_parameter("max_speed", 2.0)
        self.declare_parameter("command_topic", "turtle1/cmd_vel")
        self.pose = None
        self.busy = threading.Lock()
        self.create_subscription(Pose, "turtle1/pose", self.on_pose, 10)
        self.publisher = self.create_publisher(
            Twist, self.get_parameter("command_topic").value, 10
        )
        self.server = ActionServer(
            self,
            Navigate,
            "navigate",
            self.execute,
            callback_group=ReentrantCallbackGroup(),
            goal_callback=self.accept,
            cancel_callback=lambda _: CancelResponse.ACCEPT,
        )

    def on_pose(self, msg):
        self.pose = msg

    def accept(self, goal):
        if not all(math.isfinite(v) for v in [goal.x, goal.y, goal.timeout_seconds]):
            return GoalResponse.REJECT
        if not (
            0.5 <= goal.x <= 10.5
            and 0.5 <= goal.y <= 10.5
            and 0 < goal.timeout_seconds <= 30
        ):
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT if self.busy.acquire(False) else GoalResponse.REJECT

    def execute(self, handle):
        result = Navigate.Result()
        result.final_error = 1000.0
        start = time.monotonic()
        try:
            while (
                rclpy.ok() and time.monotonic() - start < handle.request.timeout_seconds
            ):
                if handle.is_cancel_requested:
                    handle.canceled()
                    result.message = "Canceled"
                    return result
                p = self.pose
                if p is None:
                    time.sleep(0.05)
                    continue
                dx, dy = handle.request.x - p.x, handle.request.y - p.y
                distance = math.hypot(dx, dy)
                result.final_error = distance
                if distance < 0.18:
                    handle.succeed()
                    result.success = True
                    result.message = "Target reached"
                    return result
                heading = math.atan2(dy, dx) - p.theta
                heading = math.atan2(math.sin(heading), math.cos(heading))
                speed = max(0.0, min(3.0, float(self.get_parameter("max_speed").value)))
                cmd = Twist()
                cmd.linear.x = min(speed, distance * 1.8) * max(0, math.cos(heading))
                cmd.angular.z = max(-4.0, min(4.0, heading * 5))
                self.publisher.publish(cmd)
                feedback = Navigate.Feedback()
                feedback.distance_remaining = distance
                handle.publish_feedback(feedback)
                time.sleep(0.05)
            handle.abort()
            result.message = "Deadline exceeded"
            return result
        finally:
            self.publisher.publish(Twist())
            self.busy.release()


def main():
    rclpy.init()
    n = Controller()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(n)
    try:
        executor.spin()
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        executor.shutdown()
        n.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
