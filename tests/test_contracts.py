import json
import threading
import time

import pytest

from robot_debug_lab.agents import ModelError, RobotAgent, infer
from robot_debug_lab.config import robot_id
from robot_debug_lab.store import Store


class Observations:
    def snapshot(self, rid):
        return {
            "robot_id": rid,
            "nodes": ["/amcl", "/controller_server"],
            "topics": self.topic_health(rid),
        }

    def parameters(self, rid):
        return {"scan_input": "/scan_faulted", **self.topic_health(rid)}

    def topic_health(self, rid):
        return {"raw_scan_hz": 10.0, "nav2_scan_hz": 0.0}

    def sample(self, rid):
        return {"displacement": 0.0}


def test_store_marks_unfinished_work_interrupted(tmp_path):
    path = tmp_path / "runs.sqlite"
    store = Store(path)
    unfinished = store.new("robot_1", "rules", "check")
    done = store.new("robot_1", "rules", "done")
    done["status"] = "verified"
    store.save(done)
    reopened = Store(path)
    assert reopened.get(unfinished["id"])["status"] == "interrupted"
    assert reopened.get(done["id"])["status"] == "verified"


def test_agent_tools_are_read_only_and_robot_scoped(tmp_path):
    store = Store(tmp_path / "runs.sqlite")
    run = store.new("robot_1", "rules", "check")
    agent = RobotAgent(Observations(), store, robot="robot_1")
    with pytest.raises(ValueError):
        agent.tool("repair_scan", run)
    assert agent.tool("inspect_graph", run)["data"]["robot_id"] == "robot_1"


def test_free_text_model_retry_does_not_pollute_next_diagnosis_context(tmp_path):
    class FreeTextThenFinish:
        def __init__(self):
            self.round = 0

        def complete(self, messages, tools):
            self.round += 1
            if self.round == 1:
                return {"role": "assistant", "content": "INVALID_BLOAT" * 1000}, {
                    "prompt_tokens": 10, "completion_tokens": 1000,
                }
            assert not any(
                "INVALID_BLOAT" in str(message.get("content", ""))
                for message in messages
            )
            if self.round == 2:
                return {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "parameters",
                        "type": "function",
                        "function": {"name": "read_parameters", "arguments": "{}"},
                    }],
                }, {"prompt_tokens": 20, "completion_tokens": 10}
            evidence_id = json.loads(messages[-1]["content"])["id"]
            return {
                "role": "assistant",
                "tool_calls": [{
                    "id": "finish",
                    "type": "function",
                    "function": {
                        "name": "finish_diagnosis",
                        "arguments": (
                            '{"cause":"scan_interrupted","summary":"链路中断",'
                                f'"evidence_ids":["{evidence_id}"],'
                                '"repair_rationale":"恢复转发"}'
                        ),
                    },
                }],
            }, {"prompt_tokens": 20, "completion_tokens": 10}

    store = Store(tmp_path / "retry.sqlite")
    run = store.new("robot_1", "ai", "调查导航失败")
    model = FreeTextThenFinish()
    agent = RobotAgent(Observations(), store, model=model, robot="robot_1")

    result = agent.diagnose(run, threading.Event())

    assert result["cause"] == "scan_interrupted"
    assert model.round == 3
    warnings = [event for event in run["events"] if event["kind"] == "warning"]
    assert warnings[-1]["data"]["fallback"] is False


@pytest.mark.parametrize("value", ["robot_2", "../robot_1", ""])
def test_invalid_robot_scope_rejected(value):
    with pytest.raises(ValueError):
        robot_id(value)


def test_scan_interruption_requires_live_raw_sensor_and_dead_nav2_path():
    graph = {"topics": {"raw_scan_hz": 10.0, "nav2_scan_hz": 0.0}}
    assert infer(graph, {}) == "scan_interrupted"
    graph["topics"]["nav2_scan_hz"] = 10.0
    assert infer(graph, {}) == "healthy"
    graph["topics"]["raw_scan_hz"] = 0.0
    assert infer(graph, {}) == "unknown"


def test_lifecycle_and_controller_faults_have_distinct_evidence():
    healthy_graph = {
        "topics": {"raw_scan_hz": 10.0, "nav2_scan_hz": 10.0},
        "nav2_lifecycle": {"bt_navigator": {"label": "active"}},
    }
    lifecycle = {"nav2_lifecycle": {"bt_navigator": {"label": "inactive"}}}
    assert infer(healthy_graph, lifecycle) == "navigator_inactive"
    controller = {
        "nav2_lifecycle": {"bt_navigator": {"label": "active"}},
        "controller": {"vx_max": 0.0, "vx_min": 0.0},
    }
    assert infer(healthy_graph, controller) == "controller_velocity_zero"


def test_fault_evaluation_counts_wrong_repairs_tools_and_tokens():
    from robot_debug_lab.evaluation import evaluate_run

    run = {
        "status": "verified",
        "diagnosis": {"cause": "navigator_inactive"},
        "validation": {"success": True},
        "usage": {"prompt_tokens": 12, "completion_tokens": 4},
        "events": [
            {"kind": "tool", "data": {}},
            {"kind": "repair", "data": {"operation": "restore_scan_forwarding"}},
            {"kind": "repair", "data": {"operation": "resume_nav2_lifecycle"}},
        ],
    }
    result = evaluate_run(run, "navigator_inactive", "ai", 1.2345)
    assert result["identified_correctly"] is True
    assert result["original_task_restored"] is True
    assert result["wrong_repairs"] == 1
    assert result["tool_calls"] == 1
    assert result["investigation_and_recovery_seconds"] == 1.234
    assert result["prompt_tokens"] == 12


def test_mid_mission_evaluation_separates_planning_and_diagnosis_usage():
    from robot_debug_lab.evaluation import evaluate_mission_run

    run = {
        "status": "verified",
        "created": 1.0,
        "finished": 12.0,
        "planning_usage": {"prompt_tokens": 100, "completion_tokens": 20},
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        "events": [
            {"kind": "tool", "title": "inspect_graph"},
            {"kind": "tool", "title": "read_parameters"},
            {"kind": "tool", "title": "list_approach_options"},
        ],
        "steps": [
            {"id": "S1", "status": "completed", "started": 1.0, "finished": 2.0},
            {
                "id": "S2", "status": "completed", "started": 2.0, "finished": 10.0,
                "diagnosis": {"cause": "navigator_inactive"},
                "repair": {"operation": "resume_nav2_lifecycle"},
            },
            {"id": "S3", "status": "completed", "started": 10.0, "finished": 12.0},
        ],
    }
    result = evaluate_mission_run(run, "navigator_inactive", "ai")

    assert result["identified_correctly"] is True
    assert result["original_mission_restored"] is True
    assert result["completed_steps_preserved"] == ["S1"]
    assert result["recovered_step_id"] == "S2"
    assert result["wrong_repairs"] == 0
    assert result["diagnostic_tool_calls"] == 2
    assert result["investigation_and_recovery_seconds"] == 8.0
    assert result["planning_prompt_tokens"] == 100
    assert result["diagnosis_prompt_tokens"] == 50


def test_repeated_mission_evaluation_summary_groups_mode_and_fault():
    from robot_debug_lab.evaluation import summarize_mission_rows

    base = {
        "fault": "scan_interrupted",
        "identified_correctly": True,
        "original_mission_restored": True,
        "wrong_repairs": 0,
        "diagnostic_tool_calls": 4,
        "investigation_and_recovery_seconds": 10.0,
        "diagnosis_prompt_tokens": 100,
        "diagnosis_completion_tokens": 20,
    }
    rows = [
        {**base, "recovery_mode": "ai"},
        {
            **base, "recovery_mode": "ai", "diagnostic_tool_calls": 2,
            "investigation_and_recovery_seconds": 14.0,
            "diagnosis_prompt_tokens": 80,
        },
        {
            **base, "recovery_mode": "rules", "diagnostic_tool_calls": 2,
            "diagnosis_prompt_tokens": 0, "diagnosis_completion_tokens": 0,
        },
    ]

    summary = summarize_mission_rows(rows)
    ai = next(row for row in summary if row["recovery_mode"] == "ai")
    rules = next(row for row in summary if row["recovery_mode"] == "rules")
    assert ai["trials"] == 2
    assert ai["identified_rate"] == 1.0
    assert ai["mission_restore_rate"] == 1.0
    assert ai["average_tool_calls"] == 3.0
    assert ai["average_recovery_seconds"] == 12.0
    assert ai["average_diagnosis_tokens"] == 110.0
    assert rules["average_diagnosis_tokens"] == 0.0


def test_nav2_parameter_rewrite_only_changes_exact_scan_inputs():
    from robot_debug_lab.nav2_bridge import rewrite_nav2_params

    original = """amcl:\n  scan_topic: scan\nlayer:\n  topic: \"scan\"\n  backup_topic: scan_archive\n"""
    rewritten = rewrite_nav2_params(original)
    assert "scan_topic: scan" in rewritten
    assert "topic: scan_faulted" in rewritten
    assert "backup_topic: scan_archive" in rewritten
    assert "scan_faulted_faulted" not in rewritten


class FabricatedEvidence:
    def complete(self, messages, tools):
        return {
            "role": "assistant",
            "tool_calls": [{
                "id": "bad",
                "type": "function",
                "function": {
                    "name": "finish_diagnosis",
                    "arguments": '{"cause":"scan_interrupted","summary":"test","repair_rationale":"test","evidence_ids":["E999"]}',
                },
            }],
        }, {}


def test_model_cannot_finish_with_fabricated_citations(tmp_path):
    store = Store(tmp_path / "runs.sqlite")
    run = store.new("robot_1", "ai", "check")
    run["baseline"] = {}
    with pytest.raises(ModelError, match="6-round"):
        RobotAgent(Observations(), store, FabricatedEvidence()).diagnose(
            run, threading.Event()
        )


def test_independent_verdict_uses_fresh_gazebo_odometry():
    from robot_debug_lab.nav2_bridge import Nav2Bridge

    bridge = Nav2Bridge.__new__(Nav2Bridge)
    bridge.data_lock = threading.Lock()
    bridge.pose = None
    bridge.ground_truth = {
        "x": 2.0,
        "y": 0.5,
        "observed": time.time() - 5,
        "source": "gazebo_odometry",
    }
    assert not bridge.verdict("robot_1", 2.0, 0.5, time.monotonic(), "4")["success"]
    bridge.ground_truth["observed"] = time.time()
    assert bridge.verdict("robot_1", 2.0, 0.5, time.monotonic(), "4")["success"]
    bridge.ground_truth["x"] = 1.0
    assert not bridge.verdict("robot_1", 2.0, 0.5, time.monotonic(), "4")["success"]


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
