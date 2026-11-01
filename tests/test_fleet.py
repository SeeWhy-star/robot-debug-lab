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
