from copy import deepcopy
from types import SimpleNamespace

import pytest

from robot_debug_lab.app import (
    AssistantRequest,
    _dispatch_plan,
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
