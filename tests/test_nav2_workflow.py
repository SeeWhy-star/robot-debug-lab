import json
import threading
import time

import pytest

from robot_debug_lab.agents import ModelError, RobotAgent
from robot_debug_lab.store import Store
from robot_debug_lab.workflow import Workflow


class Nav2IncidentBridge:
    robot_ids = ("robot_1",)

    def __init__(self):
        self.locks = {"robot_1": threading.Lock()}
        self.interrupted = True
        self.moves = []
        self.repairs = []

    def navigate(self, robot, x, y, cancel=None):
        self.moves.append((robot, x, y))
        return {
            "success": not self.interrupted,
            "error": 0.12 if not self.interrupted else 4.1,
            "target": [x, y],
            "message": "reached" if not self.interrupted else "navigation_timeout",
        }

    def topic_health(self, robot):
        return {
            "raw_scan_hz": 10.0,
            "nav2_scan_hz": 0.0 if self.interrupted else 10.0,
            "scan_forwarding": not self.interrupted,
        }

    def snapshot(self, robot):
        return {
            "robot_id": robot,
            "nodes": ["/amcl", "/controller_server", "/planner_server"],
            "topics": self.topic_health(robot),
        }

    def parameters(self, robot):
        return {"scan_input": "/scan_faulted", **self.topic_health(robot)}

    def sample(self, robot):
        return {"displacement": 0.0}

    def repair(self, robot, cause):
        assert cause == "scan_interrupted"
        self.repairs.append((robot, cause))
        self.interrupted = False
        return {"operation": "restore_scan_forwarding"}

    def stop(self, robot):
        pass


class InvestigatingModel:
    def __init__(self):
        self.round = 0

    def complete(self, messages, tools):
        self.round += 1
        if self.round == 1:
            return self._call("measure_topics", "measure"), {}
        if self.round == 2:
            return self._call("read_parameters", "params"), {}

        evidence_ids = []
        for message in messages:
            if message.get("role") == "tool":
                result = json.loads(message["content"])
                if "id" in result:
                    evidence_ids.append(result["id"])
        return self._call(
            "finish_diagnosis",
            "finish",
            {
                "cause": "scan_interrupted",
                "summary": "原始雷达保持 10 Hz，但 Nav2 输入为 0 Hz，导航链路中断。",
                "evidence_ids": evidence_ids,
                "repair_rationale": "恢复受控雷达转发，再重放同一导航任务。",
            },
        ), {}

    @staticmethod
    def _call(name, call_id, arguments=None):
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments or {}, ensure_ascii=False),
                    },
                }
            ],
        }


def test_ai_investigates_repairs_and_replays_nav2_incident(tmp_path):
    bridge = Nav2IncidentBridge()
    store = Store(tmp_path / "runs.db")
    flow = Workflow(bridge, store)
    model = InvestigatingModel()
    flow.agents["robot_1"] = RobotAgent(
        bridge, store, model=model, robot="robot_1"
    )

    run = flow.submit_many(
        [
            {
                "robot_id": "robot_1",
                "x": 2.0,
                "y": 0.5,
                "auto_repair": True,
            }
        ],
        "ai",
        "排查为什么机器人无法到达货架 A。",
        plan={"id": "warehouse-plan"},
    )[0]
    flow.pool.shutdown(wait=True)

    result = flow.store.get(run["id"])
    assert result["status"] == "verified"
    assert result["baseline"]["success"] is False
    assert result["diagnosis"]["cause"] == "scan_interrupted"
    assert result["validation"]["success"] is True
    assert bridge.moves == [("robot_1", 2.0, 0.5)] * 2
    assert bridge.repairs == [("robot_1", "scan_interrupted")]
    assert model.round == 3
    titles = [event["title"] for event in result["events"]]
    assert "measure_topics" in titles
    assert "read_parameters" in titles


def test_one_invalid_gateway_response_is_retried_without_rules_fallback(tmp_path):
    class FlakyModel(InvestigatingModel):
        def __init__(self):
            super().__init__()
            self.failed_once = False

        def complete(self, messages, tools):
            if not self.failed_once:
                self.failed_once = True
                raise ModelError(
                    "OpenFang response must contain valid gateway tool requests"
                )
            return super().complete(messages, tools)

    bridge = Nav2IncidentBridge()
    store = Store(tmp_path / "runs.db")
    flow = Workflow(bridge, store)
    model = FlakyModel()
    flow.agents["robot_1"] = RobotAgent(
        bridge, store, model=model, robot="robot_1"
    )
    run = flow.submit_many(
        [
            {
                "robot_id": "robot_1",
                "x": 2.0,
                "y": 0.5,
                "auto_repair": True,
            }
        ],
        "ai",
        "排查为什么机器人无法到达货架 A。",
    )[0]
    flow.pool.shutdown(wait=True)
    result = flow.store.get(run["id"])
    assert result["status"] == "verified"
    warnings = [event for event in result["events"] if event["kind"] == "warning"]
    assert warnings[0]["data"]["fallback"] is False


def mission_plan(steps):
    return {
        "id": "mission-plan",
        "request_text": "multi step",
        "backend": "deterministic",
        "steps": steps,
    }


def test_multistep_mission_runs_in_order_with_observable_simulated_charge(tmp_path):
    bridge = Nav2IncidentBridge()
    bridge.interrupted = False
    flow = Workflow(bridge, Store(tmp_path / "runs.db"))
    steps = [
        {
            "id": "S1", "action": "navigate", "destination_id": "shelf_a",
            "x": 2.0, "y": 0.5, "duration_seconds": None, "description": "前往货架 A",
            "auto_repair": True, "status": "pending",
        },
        {
            "id": "S2", "action": "wait", "destination_id": None,
            "x": None, "y": None, "duration_seconds": 0.01, "description": "等待",
            "auto_repair": True, "status": "pending",
        },
        {
            "id": "S3", "action": "simulate_charge", "destination_id": None,
            "x": None, "y": None, "duration_seconds": 0.01, "description": "模拟充电",
            "auto_repair": True, "status": "pending",
        },
        {
            "id": "S4", "action": "return_to_start", "destination_id": "start",
            "x": -2.0, "y": -0.5, "duration_seconds": None, "description": "返回起点",
            "auto_repair": True, "status": "pending",
        },
        {
            "id": "S5", "action": "enter_idle", "destination_id": None,
            "x": None, "y": None, "duration_seconds": None, "description": "进入空闲",
            "auto_repair": True, "status": "pending",
        },
    ]
    run = flow.submit_mission(mission_plan(steps))["id"]
    flow.pool.shutdown(wait=True)
    result = flow.store.get(run)
    assert result["status"] == "verified"
    assert [step["status"] for step in result["steps"]] == ["completed"] * 5
    assert bridge.moves == [("robot_1", 2.0, 0.5), ("robot_1", -2.0, -0.5)]
    kinds = [event["kind"] for event in result["events"]]
    assert "charging_started" in kinds and "charging_completed" in kinds
    assert result["mission_acceptance"]["completed_steps"] == 5
    assert result["active_target"] is None


def test_multistep_wait_can_be_canceled_and_later_steps_do_not_run(tmp_path):
    bridge = Nav2IncidentBridge()
    bridge.interrupted = False
    flow = Workflow(bridge, Store(tmp_path / "runs.db"))
    steps = [
        {
            "id": "S1", "action": "wait", "destination_id": None, "x": None, "y": None,
            "duration_seconds": 5.0, "description": "等待", "auto_repair": True,
            "status": "pending",
        },
        {
            "id": "S2", "action": "navigate", "destination_id": "shelf_a",
            "x": 2.0, "y": 0.5, "duration_seconds": None, "description": "前往货架",
            "auto_repair": True, "status": "pending",
        },
    ]
    run = flow.submit_mission(mission_plan(steps))["id"]
    deadline = time.monotonic() + 2
    while flow.store.get(run)["status"] != "waiting" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert flow.cancel(run)
    flow.pool.shutdown(wait=True)
    result = flow.store.get(run)
    assert result["status"] == "canceled"
    assert result["steps"][0]["status"] == "canceled"
    assert result["steps"][1]["status"] == "pending"
    assert bridge.moves == []


class MultiMechanismBridge(Nav2IncidentBridge):
    def __init__(self, cause):
        super().__init__()
        self.interrupted = False
        self.cause = cause

    def navigate(self, robot, x, y, cancel=None):
        self.moves.append((robot, x, y))
        return {
            "success": self.cause is None,
            "error": 0.1 if self.cause is None else 3.0,
            "target": [x, y],
            "message": "reached" if self.cause is None else "navigation_failed",
        }

    def snapshot(self, robot):
        return {
            "robot_id": robot,
            "topics": {"raw_scan_hz": 10.0, "nav2_scan_hz": 10.0},
            "nav2_lifecycle": {
                "bt_navigator": {
                    "label": "inactive" if self.cause == "navigator_inactive" else "active"
                }
            },
        }

    def parameters(self, robot):
        data = self.snapshot(robot)
        data["controller"] = {
            "vx_max": 0.0 if self.cause == "controller_velocity_zero" else 0.5,
            "vx_min": 0.0 if self.cause == "controller_velocity_zero" else -0.35,
        }
        return data

    def repair(self, robot, cause):
        assert cause == self.cause
        self.repairs.append((robot, cause))
        self.cause = None
        return {"operation": {
            "navigator_inactive": "resume_nav2_lifecycle",
            "controller_velocity_zero": "restore_controller_velocity_limits",
        }[cause]}


class MechanismModel:
    def __init__(self, cause):
        self.cause = cause
        self.round = 0

    def complete(self, messages, tools):
        self.round += 1
        if self.round == 1:
            return InvestigatingModel._call("read_parameters", "params"), {}
        evidence_ids = []
        for message in messages:
            if message.get("role") == "tool":
                result = json.loads(message["content"])
                if "id" in result:
                    evidence_ids.append(result["id"])
        return InvestigatingModel._call(
            "finish_diagnosis",
            "finish",
            {
                "cause": self.cause,
                "summary": "现场机制证据与该故障一致。",
                "evidence_ids": evidence_ids,
                "repair_rationale": "执行对应的白名单恢复并重放原任务。",
            },
        ), {}


@pytest.mark.parametrize("cause", ["navigator_inactive", "controller_velocity_zero"])
def test_distinct_nav2_faults_require_matching_evidence_and_repair(tmp_path, cause):
    bridge = MultiMechanismBridge(cause)
    store = Store(tmp_path / f"{cause}.db")
    flow = Workflow(bridge, store)
    model = MechanismModel(cause)
    flow.agents["robot_1"] = RobotAgent(bridge, store, model=model, robot="robot_1")
    run = flow.submit_many(
        [{"robot_id": "robot_1", "x": 2.0, "y": 0.5, "auto_repair": True}],
        "ai", "diagnose",
    )[0]
    flow.pool.shutdown(wait=True)
    result = store.get(run["id"])
    assert result["status"] == "verified"
    assert result["diagnosis"]["cause"] == cause
    assert bridge.repairs == [("robot_1", cause)]
    assert bridge.moves == [("robot_1", 2.0, 0.5)] * 2
