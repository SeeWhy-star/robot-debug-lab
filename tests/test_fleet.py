import json
import threading
import time

import pytest

from robot_debug_lab.fleet import FleetSupervisor
from robot_debug_lab.semantic_scene import SemanticScene
from robot_debug_lab.store import Store
from robot_debug_lab.supervisor import Supervisor
from robot_debug_lab.workflow import Workflow


class FleetBridge:
    robot_ids = ("robot_1", "robot_2")

    def __init__(self):
        self.locks = {rid: threading.Lock() for rid in self.robot_ids}
        self.moves = []

    def public(self):
        return [
            {
                "id": "robot_1", "pose": {"x": 1.5, "y": 0.5}, "busy": False,
                "scan": {"raw_scan_hz": 5.0, "nav2_scan_hz": 5.0},
                "localization": {"status": "aligned", "position_error": 0.03},
            },
            {
                "id": "robot_2", "pose": {"x": -3.5, "y": -2.5}, "busy": False,
                "scan": {"raw_scan_hz": 5.0, "nav2_scan_hz": 5.0},
                "localization": {"status": "aligned", "position_error": 0.04},
            },
        ]

    def navigate(self, robot, x, y, cancel=None):
        self.moves.append((robot, x, y))
        return {
            "success": True, "error": 0.02, "elapsed": 0.01,
            "target": [x, y], "pose": {"x": x, "y": y}, "message": "4",
        }

    def stop(self, robot):
        return None


class OneRobotFails(FleetBridge):
    def __init__(self):
        super().__init__()
        self.failed = False

    def navigate(self, robot, x, y, cancel=None):
        if robot == "robot_1" and not self.failed:
            self.failed = True
            raise RuntimeError("robot_1 controller unavailable")
        return super().navigate(robot, x, y, cancel)


class ConflictingFleetBridge(FleetBridge):
    def __init__(self):
        super().__init__()
        self.motion_lock = threading.Lock()
        self.active_motion = 0
        self.max_active_motion = 0

    def plan_path(self, robot, x, y, include_path=False):
        return {
            "reachable": True,
            "target": [x, y],
            "path_length": 2.0,
            "pose_count": 10,
            "reason": "path_found",
            "path": [[-1.0, 0.0], [1.0, 0.0]],
        }

    def navigate(self, robot, x, y, cancel=None):
        with self.motion_lock:
            self.active_motion += 1
            self.max_active_motion = max(self.max_active_motion, self.active_motion)
        time.sleep(0.08)
        try:
            return super().navigate(robot, x, y, cancel)
        finally:
            with self.motion_lock:
                self.active_motion -= 1


class FleetModel:
    def __init__(self, assignments=None):
        self.round = 0
        self.assignments = assignments or [
            {
                "robot_id": "robot_1",
                "steps": [{
                    "action": "navigate", "destination_id": "shelf_a",
                    "x": None, "y": None, "duration_seconds": None,
                    "description": "前往货架 A",
                }],
                "rationale": "Robot 01 距离货架 A 更近",
            },
            {
                "robot_id": "robot_2",
                "steps": [{
                    "action": "navigate", "destination_id": "charging_station",
                    "x": None, "y": None, "duration_seconds": None,
                    "description": "前往充电站",
                }],
                "rationale": "Robot 02 距离充电站更近",
            },
        ]

    @staticmethod
    def call(name, args, call_id=None):
        return {
            "id": call_id or name,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        }

    def complete(self, messages, tools):
        self.round += 1
        if self.round == 1:
            return {
                "role": "assistant",
                "tool_calls": [
                    self.call("get_fleet_state", {}, "fleet"),
                    self.call(
                        "resolve_destinations",
                        {"names": ["货架 A", "充电站"]}, "scene",
                    ),
                ],
            }, {"prompt_tokens": 20, "completion_tokens": 5}
        return {
            "role": "assistant",
            "tool_calls": [self.call("finish_fleet_plan", {
                "assignments": self.assignments,
                "message": "根据实时位置并行分配两项任务。",
            })],
        }, {"prompt_tokens": 30, "completion_tokens": 10}


class CoordinateFleetModel(FleetModel):
    def complete(self, messages, tools):
        self.round += 1
        if self.round == 1:
            return {
                "role": "assistant",
                "tool_calls": [self.call("get_fleet_state", {}, "fleet")],
            }, {"prompt_tokens": 10, "completion_tokens": 3}
        assignments = [
            {
                "robot_id": "robot_1",
                "steps": [{
                    "action": "navigate", "destination_id": None,
                    "x": 2.0, "y": 0.5, "duration_seconds": None,
                    "description": "前往明确坐标",
                }],
                "rationale": "用户明确指定Robot 01",
            },
            {
                "robot_id": "robot_2",
                "steps": [{
                    "action": "navigate", "destination_id": None,
                    "x": 2.0, "y": -0.5, "duration_seconds": None,
                    "description": "前往明确坐标",
                }],
                "rationale": "用户明确指定Robot 02",
            },
        ]
        return {
            "role": "assistant",
            "tool_calls": [self.call("finish_fleet_plan", {
                "assignments": assignments,
                "message": "保留明确坐标分工。",
            })],
        }, {"prompt_tokens": 20, "completion_tokens": 5}


class CurrentPoseQueryModel(FleetModel):
    def complete(self, messages, tools):
        self.round += 1
        if self.round == 1:
            return {
                "role": "assistant",
                "tool_calls": [
                    self.call("get_fleet_state", {}, "fleet"),
                    self.call(
                        "resolve_destinations",
                        {"names": ["实体障碍箱", "原地"]}, "scene",
                    ),
                ],
            }, {"prompt_tokens": 20, "completion_tokens": 5}
        assignments = [
            {
                "robot_id": "robot_1",
                "steps": [{
                    "action": "navigate", "destination_id": "temporary_crate",
                    "x": None, "y": None, "duration_seconds": None,
                    "description": "前往实体障碍箱旁边",
                }],
                "rationale": "执行已登记对象导航",
            },
            {
                "robot_id": "robot_2",
                "steps": [
                    {
                        "action": "wait", "destination_id": None,
                        "x": None, "y": None, "duration_seconds": 2.0,
                        "description": "原地等待",
                    },
                    {
                        "action": "enter_idle", "destination_id": None,
                        "x": None, "y": None, "duration_seconds": None,
                        "description": "进入空闲状态",
                    },
                ],
                "rationale": "原地不是场景导航目标",
            },
        ]
        return {
            "role": "assistant",
            "tool_calls": [self.call("finish_fleet_plan", {
                "assignments": assignments,
                "message": "使用实时位姿执行原地等待。",
            })],
        }, {"prompt_tokens": 20, "completion_tokens": 5}


class UnknownPlaceModel(CurrentPoseQueryModel):
    def complete(self, messages, tools):
        if self.round == 0:
            self.round += 1
            return {
                "role": "assistant",
                "tool_calls": [
                    self.call("get_fleet_state", {}, "fleet"),
                    self.call(
                        "resolve_destinations",
                        {"names": ["月球仓库"]}, "scene",
                    ),
                ],
            }, {"prompt_tokens": 20, "completion_tokens": 5}
        return super().complete(messages, tools)


def make_supervisor(tmp_path, model=None):
    store = Store(tmp_path / "fleet.sqlite")
    bridge = FleetBridge()
    mission = Supervisor(store, scene=SemanticScene())
    return store, bridge, FleetSupervisor(store, bridge, mission, model or FleetModel())


def test_fleet_agent_queries_live_state_and_materializes_two_scoped_missions(tmp_path):
    _, _, supervisor = make_supervisor(tmp_path)

    plan = supervisor.plan("让合适的机器人分别去货架 A 和充电站")

    assert plan["status"] == "ready"
    assert plan["plan_type"] == "fleet"
    assert [item["robot_id"] for item in plan["assignments"]] == ["robot_1", "robot_2"]
    assert [item["steps"][0]["x"] for item in plan["assignments"]] == [2.0, -4.0]
    assert [item["name"] for item in plan["tool_trace"]] == [
        "get_fleet_state", "resolve_destinations"
    ]
    assert plan["usage"] == {"prompt_tokens": 50, "completion_tokens": 15}


def test_fleet_agent_accepts_explicit_coordinates_after_live_state_query(tmp_path):
    store, bridge, supervisor = make_supervisor(tmp_path, CoordinateFleetModel())
    plan = supervisor.plan("Robot 01去(2,0.5)，Robot 02去(2,-0.5)")

    assert [item["name"] for item in plan["tool_trace"]] == ["get_fleet_state"]
    assert [item["steps"][0]["x"] for item in plan["assignments"]] == [2.0, 2.0]
    assert plan["assignments"][1]["steps"][0]["y"] == -0.5


def test_fleet_agent_treats_current_pose_as_wait_context_not_unknown_place(tmp_path):
    _, _, supervisor = make_supervisor(tmp_path, CurrentPoseQueryModel())

    plan = supervisor.plan("Robot 01去实体障碍箱旁边；Robot 02原地等待2秒后空闲")

    assert plan["status"] == "ready"
    assert plan["assignments"][1]["steps"][0]["action"] == "wait"
    result = plan["tool_trace"][1]["result"]
    assert result["results"][1] == {
        "resolved": False, "query": "原地", "reason": "unknown_object"
    }
    assert "destination_id=null" in result["instruction"]


def test_fleet_agent_still_rejects_genuinely_unknown_scene_object(tmp_path):
    _, _, supervisor = make_supervisor(tmp_path, UnknownPlaceModel())

    with pytest.raises(ValueError, match="未登记地点"):
        supervisor.plan("Robot 01去月球仓库；Robot 02原地等待")


def test_fleet_agent_cannot_invent_coordinates_not_present_in_request(tmp_path):
    _, _, supervisor = make_supervisor(tmp_path, CoordinateFleetModel())
    with pytest.raises(Exception, match="用户未明确提供"):
        supervisor.plan("让两台机器人去合适的位置")


def test_fleet_schema_rejects_concurrent_use_of_single_charger(tmp_path):
    charging = {
        "action": "navigate", "destination_id": "charging_station",
        "x": None, "y": None, "duration_seconds": None,
        "description": "前往充电站",
    }
    assignments = [
        {"robot_id": rid, "steps": [charging], "rationale": "需要充电"}
        for rid in ("robot_1", "robot_2")
    ]
    _, _, supervisor = make_supervisor(tmp_path, FleetModel(assignments))

    with pytest.raises(Exception, match="限定轮次"):
        supervisor.plan("让两台机器人同时去充电")


def test_fleet_dispatch_is_atomic_and_runs_both_robot_missions(tmp_path):
    store, bridge, supervisor = make_supervisor(tmp_path)
    plan = supervisor.plan("并行执行两项任务")
    workflow = Workflow(bridge, store)
    try:
        runs = workflow.submit_fleet(plan)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = [store.get(run["id"])["status"] for run in runs]
            if current == ["verified", "verified"]:
                break
            time.sleep(0.01)
        assert current == ["verified", "verified"]
        assert set(bridge.moves) == {
            ("robot_1", 2.0, 0.5),
            ("robot_2", -4.0, -3.15),
        }
        assert all(store.get(run["id"])["fleet_plan_id"] == plan["id"] for run in runs)
    finally:
        workflow.close()


def test_fleet_workflow_serializes_conflicting_nav2_routes(tmp_path):
    store = Store(tmp_path / "traffic-workflow.sqlite")
    bridge = ConflictingFleetBridge()
    mission = Supervisor(store, scene=SemanticScene())
    supervisor = FleetSupervisor(store, bridge, mission, FleetModel())
    plan = supervisor.plan("让两台机器人执行路线冲突的任务")
    workflow = Workflow(bridge, store)
    try:
        runs = workflow.submit_fleet(plan)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = [store.get(run["id"]) for run in runs]
            if all(run["status"] == "verified" for run in current):
                break
            time.sleep(0.01)
        assert all(run["status"] == "verified" for run in current)
        assert bridge.max_active_motion == 1
        kinds = [event["kind"] for run in current for event in run["events"]]
        assert "traffic_waited" in kinds
        assert kinds.count("traffic_released") == 2
    finally:
        workflow.close()


def test_fleet_dispatch_rejects_whole_batch_when_one_robot_is_busy(tmp_path):
    store, bridge, supervisor = make_supervisor(tmp_path)
    plan = supervisor.plan("并行执行两项任务")
    workflow = Workflow(bridge, store)
    bridge.locks["robot_2"].acquire()
    try:
        with pytest.raises(ValueError, match="批次未分派"):
            workflow.submit_fleet(plan)
        assert not bridge.locks["robot_1"].locked()
        assert bridge.moves == []
    finally:
        bridge.locks["robot_2"].release()
        workflow.close()


def test_failed_robot_remaining_navigation_is_handed_to_verified_peer(tmp_path):
    store = Store(tmp_path / "handoff.sqlite")
    bridge = OneRobotFails()
    mission = Supervisor(store, scene=SemanticScene())
    supervisor = FleetSupervisor(store, bridge, mission, FleetModel())
    plan = supervisor.plan("并行执行并在单机失败时接替", allow_handoff=True)
    workflow = Workflow(bridge, store)
    try:
        original = workflow.submit_fleet(plan)
        deadline = time.monotonic() + 3
        handoff_run = None
        while time.monotonic() < deadline:
            current_plan = store.get_plan(plan["id"])
            if current_plan.get("handoffs"):
                handoff_id = current_plan["handoffs"][0]["handoff_run_id"]
                handoff_run = store.get(handoff_id)
                source_events = store.get(original[0]["id"])["events"]
                if (
                    handoff_run["status"] == "verified"
                    and current_plan["handoffs"][0]["status"] == "verified"
                    and any(event["kind"] == "handoff_finished" for event in source_events)
                ):
                    break
            time.sleep(0.02)
        assert [store.get(run["id"])["status"] for run in original] == [
            "failed", "verified"
        ]
        assert handoff_run["robot_id"] == "robot_2"
        assert handoff_run["task_type"] == "fleet_handoff"
        assert handoff_run["status"] == "verified"
        assert handoff_run["steps"][0]["id"] == "H1"
        assert ("robot_2", 2.0, 0.5) in bridge.moves
        handoff_record = store.get_plan(plan["id"])["handoffs"][0]
        assert handoff_record["status"] == "verified"
        assert handoff_record["final_gazebo_error"] == 0.02
        source = store.get(original[0]["id"])
        assert any(event["kind"] == "handoff_finished" for event in source["events"])
    finally:
        workflow.close()


def test_fleet_handoff_can_be_disabled_by_request_policy(tmp_path):
    store = Store(tmp_path / "handoff-disabled.sqlite")
    bridge = OneRobotFails()
    mission = Supervisor(store, scene=SemanticScene())
    supervisor = FleetSupervisor(store, bridge, mission, FleetModel())
    plan = supervisor.plan("并行执行但不要接替", allow_handoff=False)
    workflow = Workflow(bridge, store)
    try:
        runs = workflow.submit_fleet(plan)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            statuses = [store.get(run["id"])["status"] for run in runs]
            if statuses == ["failed", "verified"]:
                break
            time.sleep(0.02)
        assert statuses == ["failed", "verified"]
        assert store.get_plan(plan["id"])["handoffs"] == []
        assert ("robot_2", 2.0, 0.5) not in bridge.moves
    finally:
        workflow.close()


def test_robot_specific_return_home_is_not_portable_between_robots():
    source = {
        "current_step": 0,
        "steps": [{
            "id": "S1", "action": "return_to_start", "x": -2.0, "y": -0.5,
            "description": "返回Robot 01起点", "auto_repair": True,
            "status": "failed",
        }],
    }
    assert Workflow._handoff_steps(source) == []
