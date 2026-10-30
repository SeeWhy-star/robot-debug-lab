import json
import threading

import pytest

from robot_debug_lab.agents import ModelError
from robot_debug_lab.mission_replanner import MissionReplanner
from robot_debug_lab.semantic_scene import SemanticScene
from robot_debug_lab.store import Store


class RouteBridge:
    def __init__(self):
        self.queries = []

    def plan_path(self, robot, x, y):
        self.queries.append((robot, x, y))
        return {
            "reachable": (x, y) == (2.0, -1.0),
            "target": [x, y],
            "path_length": 4.2,
            "reason": "path_found",
        }


class ReplanModel:
    def __init__(self, approach_id="southwest"):
        self.round = 0
        self.approach_id = approach_id

    @staticmethod
    def call(name, arguments):
        return {
            "role": "assistant",
            "tool_calls": [{
                "id": name,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }],
        }

    def complete(self, messages, tools):
        self.round += 1
        if self.round == 1:
            return self.call("get_mission_state", {}), {
                "prompt_tokens": 3, "completion_tokens": 1,
            }
        if self.round == 2:
            return self.call("list_approach_options", {"object_id": "shelf_a"}), {
                "prompt_tokens": 4, "completion_tokens": 1,
            }
        evidence = [
            json.loads(message["content"])["id"]
            for message in messages
            if message.get("role") == "tool" and "id" in json.loads(message["content"])
        ]
        return self.call("finish_replan", {
            "decision": "retry_with_approach",
            "failed_step_id": "S2",
            "approach_id": self.approach_id,
            "reason": "默认接近点实测失败，改用 Nav2 已验证可达的西南接近点。",
            "evidence_ids": evidence,
        }), {"prompt_tokens": 5, "completion_tokens": 2}


def failed_run(store):
    run = store.new("robot_1", "ai", "先完成前置任务，再去货架 A")
    run.update(
        usage={"prompt_tokens": 0, "completion_tokens": 0},
        steps=[
            {
                "id": "S1", "action": "wait", "description": "前置等待",
                "status": "completed",
            },
            {
                "id": "S2", "action": "navigate", "description": "前往货架 A",
                "destination_id": "shelf_a", "approach_id": "west",
                "x": 2.0, "y": 0.5, "status": "running",
                "baseline": {"success": False, "message": "navigation_timeout"},
                "failure_reason": "默认接近点不可达",
            },
            {
                "id": "S3", "action": "enter_idle", "description": "进入空闲",
                "status": "pending",
            },
        ],
    )
    store.save(run)
    return run


def test_agent_selects_only_observed_reachable_approach_and_preserves_mission(tmp_path):
    store = Store(tmp_path / "runs.db")
    bridge = RouteBridge()
    run = failed_run(store)
    replanner = MissionReplanner(
        store, bridge, SemanticScene(), model=ReplanModel()
    )

    result = replanner.replan(run, 1, threading.Event())

    assert result["decision"] == "retry_with_approach"
    assert result["replacement_step"]["approach_id"] == "southwest"
    assert (result["replacement_step"]["x"], result["replacement_step"]["y"]) == (
        2.0, -1.0,
    )
    assert bridge.queries == [("robot_1", 2.0, -1.0)]
    assert run["steps"][0]["status"] == "completed"
    assert run["steps"][2]["status"] == "pending"
    assert run["usage"] == {"prompt_tokens": 12, "completion_tokens": 4}


def test_agent_cannot_select_failed_or_unreachable_approach(tmp_path):
    store = Store(tmp_path / "runs.db")
    run = failed_run(store)
    replanner = MissionReplanner(
        store, RouteBridge(), SemanticScene(), model=ReplanModel("west")
    )

    with pytest.raises(ModelError, match="不可达"):
        replanner.replan(run, 1, threading.Event())
