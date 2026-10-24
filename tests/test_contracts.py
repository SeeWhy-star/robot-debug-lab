import threading
import time
import pytest
from robot_debug_lab.agents import RobotAgent, ModelError, infer
from robot_debug_lab.store import Store
from robot_debug_lab.config import robot_id


class Observations:
    def snapshot(self, rid):
        return {"robot_id": rid, "nodes": [f"/{rid}/controller"]}

    def parameters(self, rid):
        return {"max_speed": 0.0, "command_topic": "turtle1/cmd_vel"}


def test_store_marks_unfinished_work_interrupted(tmp_path):
    path = tmp_path / "runs.sqlite"
    store = Store(path)
    unfinished = store.new("robot_1", "rules", "check")
    done = store.new("robot_2", "rules", "check")
    done["status"] = "verified"
    store.save(done)
    reopened = Store(path)
    assert reopened.get(unfinished["id"])["status"] == "interrupted"
    assert reopened.get(done["id"])["status"] == "verified"


def test_agent_tools_cannot_run_mutations(tmp_path):
    store = Store(tmp_path / "runs.sqlite")
    run = store.new("robot_1", "rules", "check")
    agent = RobotAgent(Observations(), store)
    with pytest.raises(ValueError):
        agent.tool("restart_controller", run)
    assert agent.tool("inspect_graph", run)["data"]["robot_id"] == "robot_1"
    assert agent.tool("inspect_peer", run)["data"]["graph"]["robot_id"] == "robot_2"


def test_invalid_robot_scope_rejected():
    with pytest.raises(ValueError):
        robot_id("../robot_2")


@pytest.mark.parametrize(
    "nodes,params,expected",
    [
        ([], {}, "controller_stopped"),
        (
            ["/robot_1/controller"],
            {"command_topic": "orphan_velocity", "max_speed": 2},
            "topic_mismatch",
        ),
        (
            ["/robot_1/controller"],
            {"command_topic": "turtle1/cmd_vel", "max_speed": 0},
            "zero_speed",
        ),
        (["/robot_1/controller"], {"available": False}, "unknown"),
    ],
)
def test_rule_baseline(nodes, params, expected):
    assert infer({"robot_id": "robot_1", "nodes": nodes}, params) == expected


class FabricatedEvidence:
    def complete(self, messages, tools):
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "bad",
                    "type": "function",
                    "function": {
                        "name": "finish_diagnosis",
                        "arguments": '{"cause":"zero_speed","summary":"test","repair_rationale":"test","evidence_ids":["E999"]}',
                    },
                }
            ],
        }, {}


def test_model_cannot_finish_with_fabricated_citations(tmp_path):
    store = Store(tmp_path / "runs.sqlite")
    run = store.new("robot_1", "ai", "check")
    run["baseline"] = {}
    with pytest.raises(ModelError, match="6-round"):
        RobotAgent(Observations(), store, FabricatedEvidence()).diagnose(
            run, threading.Event()
        )
    assert run["diagnosis"] is None


def test_independent_verdict_requires_fresh_pose():
    from robot_debug_lab.bridge import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge.data_lock = threading.Lock()
    bridge.poses = {"robot_1": {"x": 8.0, "y": 8.0, "observed": time.time() - 5}}
    assert not bridge.verdict("robot_1", 8.0, 8.0, time.monotonic(), "Target reached")[
        "success"
    ]
    bridge.poses["robot_1"]["observed"] = time.time()
    assert bridge.verdict("robot_1", 8.0, 8.0, time.monotonic(), "Target reached")[
        "success"
    ]
    bridge.poses["robot_1"]["x"] = 4.0
    assert not bridge.verdict("robot_1", 8.0, 8.0, time.monotonic(), "Target reached")[
        "success"
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://provider.example/v1",
        "https://user:secret@provider.example",
        "https://provider.example?key=secret",
        "file:///tmp/model",
        "https://provider.example:bad",
    ],
)
def test_unsafe_model_endpoints_rejected(url):
    from robot_debug_lab.config import validate_model_url

    with pytest.raises(ValueError):
        validate_model_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://provider.example/v1",
        "http://127.0.0.1:4200/v1",
        "http://localhost:4200/v1",
        "http://[::1]:4200/v1",
    ],
)
def test_secure_or_loopback_model_endpoints_allowed(url):
    from robot_debug_lab.config import validate_model_url

    assert validate_model_url(url) == url
