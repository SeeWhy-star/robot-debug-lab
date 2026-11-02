import json
import threading
import time
from types import SimpleNamespace

import pytest

from robot_debug_lab.agents import ModelError, RobotAgent, infer
from robot_debug_lab.config import robot_id
from robot_debug_lab.store import Store
from robot_debug_lab.contracts import (
    PHASES,
    V2_EVENT_FIELDS,
    diagnosis_from_legacy,
    no_tools_input,
    public_run,
    repair_authorization,
    validate_authorization_evidence,
    validate_diagnosis,
    validate_diagnosis_evidence,
    validate_event,
)
from robot_debug_lab.metrics import build_metrics


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
                            '{"classification":"known_fault",'
                            '"known_cause":"scan_interrupted",'
                            '"suspected_subsystem":"sensor_pipeline",'
                            '"summary":"链路中断",'
                            f'"cited_evidence":["{evidence_id}"],'
                            '"ruled_out":[],"repair_requested":true,'
                            '"requested_repair":"restore_scan_forwarding",'
                            '"recommended_action":"恢复转发"}'
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


def test_both_configured_robot_scopes_are_accepted():
    assert robot_id("robot_1") == "robot_1"
    assert robot_id("robot_2") == "robot_2"


@pytest.mark.parametrize("value", ["robot_3", "../robot_1", ""])
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


def test_localization_drift_requires_measured_amcl_gazebo_divergence():
    graph = {
        "topics": {"raw_scan_hz": 10.0, "nav2_scan_hz": 10.0},
        "nav2_lifecycle": {"bt_navigator": {"label": "active"}},
        "localization_health": {
            "status": "drifted", "position_error": 0.8, "threshold": 0.45,
        },
    }
    params = {
        "nav2_lifecycle": {"bt_navigator": {"label": "active"}},
        "controller": {"vx_max": 0.5, "vx_min": -0.35},
    }
    assert infer(graph, params) == "localization_drift"
    graph["localization_health"].update(status="aligned", position_error=0.2)
    assert infer(graph, params) == "healthy"


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


def test_localization_fault_evaluation_requires_matching_reseed():
    from robot_debug_lab.evaluation import evaluate_run

    run = {
        "status": "verified",
        "diagnosis": {"cause": "localization_drift"},
        "validation": {"success": True},
        "events": [{
            "kind": "repair",
            "data": {"operation": "reseed_amcl_from_gazebo_truth"},
        }],
    }

    result = evaluate_run(run, "localization_drift", "rules")

    assert result["identified_correctly"] is True
    assert result["wrong_repairs"] == 0


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


def test_nav2_parameter_rewrite_disables_shared_amcl_pose_file_writes():
    from robot_debug_lab.nav2_bridge import rewrite_nav2_params

    rewritten = rewrite_nav2_params("amcl:\n  save_pose_rate: 0.5\n")
    assert "save_pose_rate: 0.0" in rewritten


def test_nav2_parameter_rewrite_keeps_rechecking_a_tighter_xy_goal():
    from robot_debug_lab.nav2_bridge import rewrite_nav2_params

    rewritten = rewrite_nav2_params(
        "controller:\n  general_goal_checker:\n    stateful: true\n"
        "    xy_goal_tolerance: 0.25\n"
    )

    assert "stateful: false" in rewritten
    assert "xy_goal_tolerance: 0.15" in rewritten


class FabricatedEvidence:
    def complete(self, messages, tools):
        return {
            "role": "assistant",
            "tool_calls": [{
                "id": "bad",
                "type": "function",
                "function": {
                    "name": "finish_diagnosis",
                    "arguments": (
                        '{"classification":"known_fault",'
                        '"known_cause":"scan_interrupted",'
                        '"suspected_subsystem":"sensor_pipeline",'
                        '"summary":"test","cited_evidence":["E999"],'
                        '"ruled_out":[],"repair_requested":true,'
                        '"requested_repair":"restore_scan_forwarding",'
                        '"recommended_action":"test"}'
                    ),
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
    runtime = SimpleNamespace(
        pose=None,
        ground_truth={
            "x": 2.0,
            "y": 0.5,
            "observed": time.time() - 5,
            "source": "gazebo_odometry",
        },
    )
    bridge.robots = {"robot_1": runtime}
    assert not bridge.verdict("robot_1", 2.0, 0.5, time.monotonic(), "4")["success"]
    runtime.ground_truth["observed"] = time.time()
    assert bridge.verdict("robot_1", 2.0, 0.5, time.monotonic(), "4")["success"]
    runtime.ground_truth["x"] = 1.0
    assert not bridge.verdict("robot_1", 2.0, 0.5, time.monotonic(), "4")["success"]


def test_localization_health_compares_amcl_with_gazebo_truth():
    from robot_debug_lab.nav2_bridge import Nav2Bridge

    bridge = Nav2Bridge.__new__(Nav2Bridge)
    bridge.data_lock = threading.RLock()
    runtime = SimpleNamespace(
        ground_truth={"x": 1.0, "y": 2.0, "observed": time.time()},
        pose={"x": 1.8, "y": 2.0, "observed": time.time()},
    )
    bridge.robots = {"robot_1": runtime}

    health = bridge.localization_health("robot_1")

    assert health["status"] == "drifted"
    assert health["position_error"] == 0.8
    runtime.pose["x"] = 1.1
    assert bridge.localization_health("robot_1")["status"] == "aligned"


def test_scan_repair_waits_for_stable_stream_and_clears_stale_costmaps(monkeypatch):
    from robot_debug_lab import nav2_bridge
    from robot_debug_lab.nav2_bridge import Nav2Bridge

    class ClearClient:
        def __init__(self):
            self.calls = 0

        def wait_for_service(self, timeout_sec):
            return True

        def call_async(self, request):
            self.calls += 1
            return SimpleNamespace(success=True)

    bridge = Nav2Bridge.__new__(Nav2Bridge)
    bridge.data_lock = threading.RLock()
    local, global_ = ClearClient(), ClearClient()
    runtime = SimpleNamespace(
        scan_forwarding=False,
        local_costmap_clear_client=local,
        global_costmap_clear_client=global_,
    )
    bridge.robots = {"robot_1": runtime}
    bridge.ClearEntireCostmap = SimpleNamespace(Request=lambda: object())
    bridge._wait = lambda future, timeout: future
    readings = iter([
        {"raw_scan_hz": 5.0, "nav2_scan_hz": 0.0, "scan_forwarding": True},
        {"raw_scan_hz": 5.0, "nav2_scan_hz": 0.5, "scan_forwarding": True},
        {"raw_scan_hz": 5.0, "nav2_scan_hz": 3.0, "scan_forwarding": True},
        {"raw_scan_hz": 5.0, "nav2_scan_hz": 5.0, "scan_forwarding": True},
    ])
    bridge.topic_health = lambda robot: next(readings)
    monkeypatch.setattr(nav2_bridge.time, "sleep", lambda _: None)

    result = bridge.repair("robot_1", "scan_interrupted")

    assert runtime.scan_forwarding is True
    assert result["nav2_scan_hz"] == 5.0
    assert result["stale_costmaps_cleared"] == {"local": True, "global": True}
    assert local.calls == global_.calls == 1


def test_sim_start_waits_for_active_amcl_before_seeding_then_waits_for_nav2():
    from robot_debug_lab.nav2_bridge import Nav2Bridge

    bridge = Nav2Bridge.__new__(Nav2Bridge)
    bridge.robot_ids = ("robot_1", "robot_2")
    bridge.params = object()
    bridge.sim = SimpleNamespace(poll=lambda: None)
    bridge._spawn_sim = lambda params: bridge.sim
    runtimes = {
        robot: SimpleNamespace(
            client=SimpleNamespace(wait_for_server=lambda timeout_sec: True)
        )
        for robot in bridge.robot_ids
    }
    bridge._rt = lambda robot: runtimes[robot]
    order = []
    bridge._wait_amcl_active = lambda robot: order.append(("amcl", robot))
    bridge._wait_nav2_active = lambda robot: order.append(("active", robot))
    bridge._set_initial_pose = lambda robot: order.append(("pose", robot))

    bridge._start_sim()

    assert order == [
        ("amcl", "robot_1"), ("amcl", "robot_2"),
        ("pose", "robot_1"), ("pose", "robot_2"),
        ("active", "robot_1"), ("active", "robot_2"),
    ]


def test_reset_refreshes_nav2_action_clients_before_starting_sim(monkeypatch):
    from robot_debug_lab import nav2_bridge
    from robot_debug_lab.nav2_bridge import Nav2Bridge

    created = []

    class OldClient:
        def __init__(self, name):
            self.name = name
            self.destroyed = False

        def destroy(self):
            self.destroyed = True

    class NewClient:
        def __init__(self, node, action_type, name):
            self.node = node
            self.action_type = action_type
            self.name = name
            created.append(name)

    bridge = Nav2Bridge.__new__(Nav2Bridge)
    bridge.robot_ids = ("robot_1", "robot_2")
    bridge.node = object()
    bridge.NavigateToPose = object()
    bridge.ComputePathToPose = object()
    runtimes = {
        robot: SimpleNamespace(
            client=OldClient(f"{robot}-navigate"),
            path_client=OldClient(f"{robot}-path"),
        )
        for robot in bridge.robot_ids
    }
    old_clients = [
        client
        for runtime in runtimes.values()
        for client in (runtime.client, runtime.path_client)
    ]
    bridge._rt = lambda robot: runtimes[robot]
    monkeypatch.setattr(nav2_bridge, "ActionClient", NewClient)

    bridge._refresh_action_clients()

    assert all(client.destroyed for client in old_clients)
    assert created == [
        "/robot_1/navigate_to_pose", "/robot_1/compute_path_to_pose",
        "/robot_2/navigate_to_pose", "/robot_2/compute_path_to_pose",
    ]


def test_sim_restart_refreshes_clients_then_waits_for_old_action_endpoints():
    from robot_debug_lab.nav2_bridge import Nav2Bridge

    class Client:
        def __init__(self, states):
            self.states = iter(states)
            self.last = True

        def server_is_ready(self):
            self.last = next(self.states, self.last)
            return self.last

    bridge = Nav2Bridge.__new__(Nav2Bridge)
    bridge.robots = {}
    order = []
    bridge._stop_sim = lambda: order.append("stopped")

    def refresh():
        order.append("refreshed")
        bridge.robots = {
            "robot_1": SimpleNamespace(
                client=Client([True, False]), path_client=Client([False, False])
            ),
            "robot_2": SimpleNamespace(
                client=Client([False, False]), path_client=Client([False, False])
            ),
        }

    bridge._refresh_action_clients = refresh

    bridge._prepare_sim_restart()

    assert order == ["stopped", "refreshed"]


def test_sim_restart_fails_closed_when_old_action_endpoints_remain(monkeypatch):
    from robot_debug_lab import nav2_bridge
    from robot_debug_lab.nav2_bridge import Nav2Bridge

    ready = SimpleNamespace(server_is_ready=lambda: True)
    bridge = Nav2Bridge.__new__(Nav2Bridge)
    bridge.robots = {
        "robot_1": SimpleNamespace(client=ready, path_client=ready),
    }
    bridge._stop_sim = lambda: None
    refreshed = []
    bridge._refresh_action_clients = lambda: refreshed.append(True)
    ticks = iter([0.0, 0.0, 26.0])
    monkeypatch.setattr(nav2_bridge.time, "monotonic", lambda: next(ticks, 26.0))
    monkeypatch.setattr(nav2_bridge.time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="旧Nav2 action端点"):
        bridge._prepare_sim_restart()
    assert refreshed == [True]


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


def test_v2_phase_transitions_are_persisted_and_legacy_runs_are_mapped(tmp_path):
    store = Store(tmp_path / "phase.sqlite")
    run = store.new("robot_1", "rules", "check")
    store.transition(run, "navigating", "start")
    store.transition(run, "investigating", "navigation_failed")
    store.transition(run, "escalated", "no_repair")

    restored = public_run(store.get(run["id"]))
    assert restored["phase"] == "escalated"
    assert restored["terminal"] is True
    assert [e["data"]["to"] for e in restored["events"]] == [
        "navigating", "investigating", "escalated"
    ]
    with pytest.raises(ValueError, match="Terminal phase"):
        store.transition(run, "navigating", "illegal_restart")

    legacy = store.new("robot_2", "rules", "legacy")
    legacy.update(status="simulated_charging")
    assert public_run(legacy)["phase"] == "executing"
    assert set(PHASES) >= {"planned", "replaying", "nav2_ok_gazebo_failed"}


def test_v2_event_schema_requires_fixed_fields():
    for kind, fields in V2_EVENT_FIELDS.items():
        with pytest.raises(ValueError, match="Missing event fields"):
            validate_event(kind, {})
        assert fields
    with pytest.raises(ValueError, match="Unknown v2 event"):
        validate_event("invented", {})


def _navigation_evidence(store, run, *, robot_id=None):
    if robot_id is not None:
        run["robot_id"] = robot_id
    return store.event_v2(run, "navigation_attempt", "导航失败", {
        "attempt": 1,
        "stage": "initial",
        "target": [2.0, 0.5],
        "nav2_success": False,
        "nav2_status": "timeout",
        "duration_seconds": 30.0,
        "final_pose": [-2.0, -0.5],
    })


def test_diagnosis_evidence_rejects_phase_other_run_and_other_robot(tmp_path):
    store = Store(tmp_path / "evidence.sqlite")
    run = store.new("robot_1", "ai", "check")
    nav = _navigation_evidence(store, run)
    diagnosis = diagnosis_from_legacy({
        "cause": "scan_interrupted",
        "summary": "雷达转发中断",
        "evidence_ids": [nav["id"]],
        "repair_rationale": "申请恢复转发",
    }, "tools_llm")
    assert validate_diagnosis_evidence(
        diagnosis, run["events"], run["id"], "robot_1"
    ) is diagnosis

    phase = store.transition(run, "navigating", "start")
    bad = {**diagnosis, "cited_evidence": [phase["id"]], "evidence_ids": [phase["id"]]}
    with pytest.raises(ValueError, match="ineligible"):
        validate_diagnosis_evidence(bad, run["events"], run["id"], "robot_1")

    other = store.new("robot_1", "ai", "other")
    other_nav = _navigation_evidence(store, other)
    bad = {
        **diagnosis,
        "cited_evidence": [other_nav["id"]],
        "evidence_ids": [other_nav["id"]],
    }
    with pytest.raises(ValueError, match="scope"):
        validate_diagnosis_evidence(
            bad, run["events"] + other["events"], run["id"], "robot_1"
        )

    foreign = store.new("robot_2", "ai", "foreign")
    foreign_nav = _navigation_evidence(store, foreign)
    bad = {
        **diagnosis,
        "cited_evidence": [foreign_nav["id"]],
        "evidence_ids": [foreign_nav["id"]],
    }
    with pytest.raises(ValueError, match="scope"):
        validate_diagnosis_evidence(
            bad, run["events"] + foreign["events"], run["id"], "robot_1"
        )


def test_no_tools_input_and_citations_are_exactly_limited(tmp_path):
    store = Store(tmp_path / "no-tools.sqlite")
    run = store.new("robot_1", "ai", "导航到目标点")
    nav = _navigation_evidence(store, run)
    payload = no_tools_input("robot_1", run["prompt"], [2.0, 0.5], nav)
    assert set(payload) == {
        "robot_id", "task_description", "target", "navigation_attempt"
    }
    assert set(payload["navigation_attempt"]) == {
        "evidence_id", "nav2_success", "nav2_status", "duration_seconds",
        "final_pose",
    }
    diagnosis = diagnosis_from_legacy({
        "cause": "unknown", "summary": "证据不足",
        "evidence_ids": [nav["id"]], "repair_rationale": "升级人工",
    }, "no_tools_llm")
    validate_diagnosis_evidence(diagnosis, run["events"], run["id"], "robot_1")

    tool = store.event_v2(run, "tool_call", "inspect_graph", {
        "tool": "inspect_graph", "actor": "diagnostic_agent",
        "input_summary": {"robot_id": "robot_1"}, "output_summary": {},
        "evidence_id": None, "duration_ms": 1, "success": True, "error": None,
    })
    bad = {**diagnosis, "cited_evidence": [tool["id"]], "evidence_ids": [tool["id"]]}
    with pytest.raises(ValueError, match="No-tools"):
        validate_diagnosis_evidence(bad, run["events"], run["id"], "robot_1")


def test_repair_authorization_is_allowlisted_and_requires_fresh_server_evidence(tmp_path):
    store = Store(tmp_path / "authorization.sqlite")
    run = store.new("robot_1", "ai", "check")
    nav = _navigation_evidence(store, run)
    diagnosis = diagnosis_from_legacy({
        "cause": "navigator_inactive", "summary": "导航器未激活",
        "evidence_ids": [nav["id"]], "repair_rationale": "恢复生命周期",
    }, "tools_llm")
    wrong = {**diagnosis, "requested_repair": "restore_scan_forwarding"}
    with pytest.raises(ValueError, match="allowlisted"):
        validate_diagnosis(wrong)

    evidence = store.event_v2(run, "tool_call", "授权复测", {
        "tool": "authorize_repair_state", "actor": "repair_authorizer",
        "input_summary": {"robot_id": "robot_1"}, "output_summary": {},
        "evidence_id": None, "duration_ms": 1, "success": True, "error": None,
    })
    authorization = repair_authorization(
        diagnosis, "navigator_inactive", [evidence["id"]]
    )
    assert authorization["decision"] == "authorized"
    validate_authorization_evidence(
        authorization, run["events"], run["id"], "robot_1"
    )
    evidence["time"] = authorization["checked_at"] - 3.0
    with pytest.raises(ValueError, match="stale"):
        validate_authorization_evidence(
            authorization, run["events"], run["id"], "robot_1"
        )

    unknown = diagnosis_from_legacy({
        "cause": "unknown", "summary": "未知", "evidence_ids": [nav["id"]],
        "repair_rationale": "人工处理",
    }, "tools_llm")
    assert repair_authorization(
        unknown, "unknown", [evidence["id"]]
    )["decision"] == "not_applicable"


def test_metrics_only_use_private_experiment_labels_and_keep_model_metadata(tmp_path):
    store = Store(tmp_path / "metrics.sqlite")

    known = store.new("robot_1", "rules", "known")
    known_diag = diagnosis_from_legacy({
        "cause": "scan_interrupted", "summary": "链路中断",
        "evidence_ids": [], "repair_rationale": "恢复转发",
    }, "rules")
    known.update(
        status="verified", phase="verified", phase_managed=True, terminal=True,
        diagnosis=known_diag, model="none", backend="rules",
        prompt_version="diagnostic-structure-v1-frozen",
    )
    store.save(known)
    store.label_experiment(
        known["id"], "exp-1", "scan_interrupted", "sensor_pipeline"
    )

    unseen = store.new("robot_1", "ai", "unseen")
    unseen_diag = diagnosis_from_legacy({
        "cause": "unknown", "summary": "疑似坐标变换子系统",
        "evidence_ids": [], "repair_rationale": "升级人工",
    }, "tools_llm")
    unseen_diag.update(
        classification="unknown_fault", suspected_subsystem="tf"
    )
    validate_diagnosis(unseen_diag)
    unseen.update(
        status="unresolved", phase="escalated", phase_managed=True, terminal=True,
        diagnosis=unseen_diag, model="test-model", backend="openfang",
        prompt_version="diagnostic-structure-v1-frozen",
    )
    store.save(unseen)
    store.label_experiment(unseen["id"], "exp-1", "tf_disconnected", "tf")

    manual = store.new("robot_2", "rules", "manual demo")
    manual.update(status="verified", phase="verified", phase_managed=True, terminal=True)
    store.save(manual)

    result = build_metrics(store, experiment_id="exp-1")
    assert result["sample"]["run_count"] == 2
    assert result["sample"]["unlabeled_runs_excluded"] == 1
    tools = next(
        row for row in result["diagnosis_by_strategy"]
        if row["strategy"] == "tools_llm"
    )
    assert tools["unknown_fault_classification_rate"] == 1.0
    assert tools["unknown_subsystem_accuracy"] == 1.0
    assert (tools["model"], tools["backend"], tools["prompt_version"]) == (
        "test-model", "openfang", "diagnostic-structure-v1-frozen"
    )
    assert "true_cause" not in json.dumps(store.get(unseen["id"]))
    assert "experiment_id" not in json.dumps(public_run(store.get(unseen["id"])))


def test_api_state_snapshot_restores_phase_timeline_and_active_runs(tmp_path, monkeypatch):
    import importlib

    app_module = importlib.import_module("robot_debug_lab.app")
    store = Store(tmp_path / "state.sqlite")
    active = store.new("robot_1", "rules", "active")
    active["active_target"] = [2.0, 0.5]
    store.transition(active, "navigating", "start")
    store.transition(active, "investigating", "failed")
    finished = store.new("robot_2", "rules", "done")
    store.transition(finished, "navigating", "start")
    store.transition(finished, "verifying", "accept")
    finished["status"] = "verified"
    store.transition(finished, "verified", "accepted")

    class Bridge:
        robot_ids = ("robot_1", "robot_2")
        locks = {"robot_1": threading.Lock(), "robot_2": threading.Lock()}

        @staticmethod
        def public():
            return []

    monkeypatch.setattr(app_module.app.state, "store", store, raising=False)
    monkeypatch.setattr(app_module.app.state, "bridge", Bridge(), raising=False)
    monkeypatch.setattr(
        app_module.app.state,
        "workflow",
        SimpleNamespace(traffic=SimpleNamespace(snapshot=lambda: {})),
        raising=False,
    )
    monkeypatch.setattr(app_module.app.state, "token", "test-token", raising=False)
    snapshot = app_module.state()

    assert snapshot["contract_version"] == "2.0"
    assert snapshot["active_run_ids"] == [active["id"]]
    current = next(run for run in snapshot["runs"] if run["id"] == active["id"])
    assert current["phase"] == "investigating"
    assert current["active_target"] is None
    assert len(current["events"]) == 2
    assert finished["id"] not in snapshot["active_run_ids"]


def test_single_robot_fault_path_emits_v2_timeline_and_restores_task(tmp_path):
    class Bridge(Observations):
        robot_ids = ("robot_1",)

        def __init__(self):
            self.locks = {"robot_1": threading.Lock()}
            self.broken = True

        def topic_health(self, rid):
            return {
                "raw_scan_hz": 5.0,
                "nav2_scan_hz": 0.0 if self.broken else 5.0,
                "scan_forwarding": not self.broken,
            }

        def navigate(self, rid, x, y, cancel=None):
            return {
                "success": not self.broken,
                "nav2_success": not self.broken,
                "error": 4.0 if self.broken else 0.05,
                "elapsed": 0.01,
                "target": [x, y],
                "pose": {"x": x if not self.broken else -2.0, "y": y if not self.broken else -0.5,
                         "observed": time.time(), "source": "gazebo_odometry"},
                "message": "navigation_timeout" if self.broken else "4",
            }

        def repair(self, rid, cause):
            assert cause == "scan_interrupted"
            self.broken = False
            return {"operation": "restore_scan_forwarding"}

        def stop(self, rid):
            pass

    from robot_debug_lab.workflow import Workflow

    bridge = Bridge()
    store = Store(tmp_path / "core-path.sqlite")
    flow = Workflow(bridge, store)
    run = flow.submit_many([
        {"robot_id": "robot_1", "x": 2.0, "y": 0.5, "auto_repair": True}
    ], "rules", "调查导航失败")[0]
    flow.pool.shutdown(wait=True)
    flow.fleet_pool.shutdown(wait=True)

    result = public_run(store.get(run["id"]))
    assert result["phase"] == "verified"
    assert result["status"] == "verified"
    assert result["repair_authorization"]["decision"] == "authorized"
    v2 = [event for event in result["events"] if event.get("schema_version") == 2]
    assert {
        "phase_changed", "navigation_attempt", "tool_call", "diagnosis",
        "repair_authorization", "repair_execution", "verification",
    } <= {event["kind"] for event in v2}
    for event in v2:
        validate_event(event["kind"], event["data"])
        assert event["run_id"] == result["id"]
        assert event["robot_id"] == "robot_1"


def test_diagnostic_prompt_v1_is_frozen_to_structure_and_normal_invariants():
    from robot_debug_lab.agents import (
        DIAGNOSTIC_PROMPT_FINGERPRINT,
        DIAGNOSTIC_SYSTEM_PROMPT,
        PROMPT_VERSION,
        TOOLS,
    )

    assert PROMPT_VERSION == "diagnostic-structure-v1-frozen"
    assert DIAGNOSTIC_PROMPT_FINGERPRINT == (
        "f08d7ca02688a8d2b90988739b135a962b49455a7c00da1100310e2b1898a8de"
    )
    assert "normal operation" in DIAGNOSTIC_SYSTEM_PROMPT.lower()
    assert "not as a list of possible faults" in DIAGNOSTIC_SYSTEM_PROMPT.lower()
    frozen_payload = json.dumps(
        {"system": DIAGNOSTIC_SYSTEM_PROMPT, "tools": TOOLS}, ensure_ascii=False
    ).lower()
    for targeted_hint in (
        "tf_disconnected", "map->odom", "missing_edge", "zero-rate",
        "supports scan_interrupted", "supports navigator_inactive",
    ):
        assert targeted_hint not in frozen_payload


def test_tools_llm_can_report_unknown_without_matching_rule_infer(tmp_path):
    class UnknownModel:
        def complete(self, messages, tools):
            initial = json.loads(messages[1]["content"])["initial_evidence"]
            arguments = {
                "classification": "unknown_fault",
                "known_cause": None,
                "suspected_subsystem": "planner",
                "summary": "导航失败，但现有证据不符合已知白名单机制。",
                "cited_evidence": [initial["id"]],
                "ruled_out": [],
                "repair_requested": False,
                "requested_repair": None,
                "recommended_action": "升级人工检查规划器现场状态。",
            }
            return {
                "role": "assistant",
                "tool_calls": [{
                    "id": "finish",
                    "type": "function",
                    "function": {
                        "name": "finish_diagnosis",
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }],
            }, {"prompt_tokens": 20, "completion_tokens": 10}

    store = Store(tmp_path / "unknown-free-diagnosis.sqlite")
    run = store.new("robot_1", "ai", "调查未知导航失败")
    run["baseline"] = {"success": False, "message": "navigation_timeout"}
    result = RobotAgent(
        Observations(), store, model=UnknownModel(), robot="robot_1"
    ).diagnose(run, threading.Event())

    # The deterministic rule would call this scan_interrupted, but the
    # diagnosis layer now validates evidence provenance rather than equality
    # with infer(). Repair authorization remains a separate later decision.
    assert infer(Observations().snapshot("robot_1"), {}) == "scan_interrupted"
    assert result["classification"] == "unknown_fault"
    assert result["known_cause"] is None
    assert result["cause"] == "unknown"
    assert result["repair_requested"] is False
