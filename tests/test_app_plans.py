from copy import deepcopy
from types import SimpleNamespace

import pytest

from robot_debug_lab.app import (
    AssistantRequest,
    _dispatch_fleet_plan,
    _dispatch_plan,
    _validate_stored_fleet_plan,
    _validate_stored_plan,
    app,
    assistant_task,
)
from robot_debug_lab.semantic_scene import SemanticScene


def stored_plan():
    return {
        "id": "123456789abc",
        "request_text": "去货架 A",
        "backend": "openfang",
        "status": "ready",
        "run_ids": [],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        "steps": [{
            "id": "S1",
            "action": "navigate",
            "destination_id": "shelf_a",
            "approach_id": "west",
            "x": 2.0,
            "y": 0.5,
            "duration_seconds": None,
            "description": "前往货架 A",
            "auto_repair": True,
            "status": "pending",
        }],
    }


class FakeStore:
    def __init__(self, plan):
        self.plan = plan

    def save_plan(self, plan):
        self.plan = deepcopy(plan)

    def get_plan(self, _):
        return deepcopy(self.plan)


class FakeWorkflow:
    def __init__(self):
        self.calls = []

    def submit_mission(self, plan, mode):
        self.calls.append((deepcopy(plan), mode))
        return {"id": f"run{len(self.calls)}"}

    def submit_fleet(self, plan, mode):
        self.calls.append((deepcopy(plan), mode))
        count = len(self.calls)
        return [{"id": f"run{count}a"}, {"id": f"run{count}b"}]


def configure_app(plan):
    store = FakeStore(plan)
    workflow = FakeWorkflow()
    app.state.store = store
    app.state.workflow = workflow
    app.state.supervisor = SimpleNamespace(
        scene=SemanticScene(),
        plan=lambda *_: deepcopy(plan),
    )
    return store, workflow


def test_plan_only_request_does_not_dispatch_robot_work():
    plan = stored_plan()
    _, workflow = configure_app(plan)

    result = assistant_task(AssistantRequest(prompt="复杂任务", execute=False))

    assert result["plan"]["id"] == plan["id"]
    assert result["run_ids"] == []
    assert workflow.calls == []


def test_stored_plan_can_run_in_both_modes_without_changing_steps():
    plan = stored_plan()
    store, workflow = configure_app(plan)

    ai = _dispatch_plan(deepcopy(plan), "ai")
    rules = _dispatch_plan(store.get_plan(plan["id"]), "rules")

    assert ai["run_ids"] == ["run1"]
    assert rules["run_ids"] == ["run2"]
    assert [mode for _, mode in workflow.calls] == ["ai", "rules"]
    assert workflow.calls[0][0]["steps"] == workflow.calls[1][0]["steps"]
    assert store.plan["run_ids"] == ["run1", "run2"]


def test_stored_semantic_plan_is_rejected_after_scene_coordinates_change():
    plan = stored_plan()
    configure_app(plan)
    changed = deepcopy(plan)
    changed["steps"][0]["x"] = 3.0

    with pytest.raises(ValueError, match="坐标已变化"):
        _validate_stored_plan(changed)


def stored_fleet_plan():
    return {
        "id": "abcdef123456",
        "plan_type": "fleet",
        "request_text": "Robot 01去货架A，Robot 02去充电站",
        "backend": "openfang",
        "status": "ready",
        "run_ids": [],
        "assignments": [
            {
                "robot_id": "robot_1",
                "rationale": "明确分工",
                "steps": [{
                    "id": "S1", "action": "navigate",
                    "destination_id": "shelf_a", "approach_id": "west",
                    "x": 2.0, "y": 0.5, "duration_seconds": None,
                    "description": "前往货架A", "auto_repair": True,
                    "status": "pending",
                }],
            },
            {
                "robot_id": "robot_2",
                "rationale": "明确分工",
                "steps": [{
                    "id": "S1", "action": "navigate",
                    "destination_id": "charging_station", "approach_id": "default",
                    "x": -4.0, "y": -3.15, "duration_seconds": None,
                    "description": "前往充电站", "auto_repair": True,
                    "status": "pending",
                }],
            },
        ],
    }


def test_stored_fleet_plan_replays_same_assignments_in_both_modes():
    plan = stored_fleet_plan()
    store, workflow = configure_app(plan)

    ai = _dispatch_fleet_plan(deepcopy(plan), "ai")
    rules = _dispatch_fleet_plan(store.get_plan(plan["id"]), "rules")

    assert ai["run_ids"] == ["run1a", "run1b"]
    assert rules["run_ids"] == ["run2a", "run2b"]
    assert [mode for _, mode in workflow.calls] == ["ai", "rules"]
    assert workflow.calls[0][0]["assignments"] == workflow.calls[1][0]["assignments"]
    assert store.plan["run_ids"] == ["run1a", "run1b", "run2a", "run2b"]


def test_stored_fleet_plan_rejects_scene_drift_and_invented_coordinates():
    plan = stored_fleet_plan()
    configure_app(plan)
    changed = deepcopy(plan)
    changed["assignments"][0]["steps"][0]["x"] = 2.5
    with pytest.raises(ValueError, match="坐标已变化"):
        _validate_stored_fleet_plan(changed)

    coordinate = deepcopy(plan)
    coordinate["assignments"][0]["steps"][0].update(
        destination_id=None, approach_id=None, x=1.0, y=1.0
    )
    with pytest.raises(ValueError, match="未经用户明确提供"):
        _validate_stored_fleet_plan(coordinate)

    malformed = deepcopy(coordinate)
    malformed["assignments"][0]["steps"][0]["x"] = None
    with pytest.raises(ValueError, match="无效坐标"):
        _validate_stored_fleet_plan(malformed)
