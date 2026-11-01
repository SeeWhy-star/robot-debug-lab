from robot_debug_lab.fleet_evaluation import evaluate_fleet_case, summarize_fleet_rows


def plan():
    return {
        "id": "fleet-plan",
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        "tool_trace": [{"name": "get_fleet_state"}, {"name": "resolve_destinations"}],
    }


def verified(robot, run_id, reservations=None):
    return {
        "id": run_id,
        "robot_id": robot,
        "status": "verified",
        "created": 1.0,
        "finished": 4.0,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "events": [],
        "validation": {"success": True, "error": 0.1},
        "mission_acceptance": {"success": True},
        "steps": [{"traffic_reservations": reservations or []}],
    }


def test_traffic_case_requires_real_path_wait_and_gazebo_acceptance():
    first = verified("robot_1", "r1", [{
        "path_mode": "nav2_geometry", "wait_seconds": 0.0, "blocked_by": [],
    }])
    second = verified("robot_2", "r2", [{
        "path_mode": "nav2_geometry", "wait_seconds": 2.5,
        "blocked_by": ["robot_1"],
    }])

    row = evaluate_fleet_case("traffic", "ai", plan(), [first, second])

    assert row["scenario_passed"] is True
    assert row["traffic_conflict_waits"] == 1
    assert row["traffic_max_wait_seconds"] == 2.5
    assert row["traffic_global_fallbacks"] == 0


def test_fault_recovery_scores_model_tools_and_wrong_repairs():
    source = verified("robot_1", "r1")
    source.update(
        diagnosis={"cause": "scan_interrupted"},
        usage={"prompt_tokens": 30, "completion_tokens": 10},
        events=[
            {"kind": "tool", "title": "measure_topics"},
            {"kind": "repair", "data": {"operation": "restore_scan_forwarding"}},
        ],
    )
    peer = verified("robot_2", "r2")

    row = evaluate_fleet_case(
        "fault_recovery", "ai", plan(), [source, peer],
        expected_fault="scan_interrupted",
    )

    assert row["scenario_passed"] is True
    assert row["fault_identified"] is True
    assert row["diagnostic_tool_calls"] == 1
    assert row["diagnosis_tokens"] == 40
    assert row["wrong_repairs"] == 0


def test_fresh_secondary_fault_repair_is_not_counted_as_wrong():
    source = verified("robot_1", "r1")
    source.update(
        diagnosis={"cause": "scan_interrupted"},
        events=[
            {"kind": "tool", "title": "measure_topics"},
            {"kind": "repair", "data": {
                "cause": "scan_interrupted",
                "operation": "restore_scan_forwarding",
            }},
            {"kind": "repair", "data": {
                "cause": "localization_drift",
                "operation": "reseed_amcl_from_gazebo_truth",
            }},
        ],
    )
    row = evaluate_fleet_case(
        "fault_recovery", "ai", plan(), [source, verified("robot_2", "r2")],
        expected_fault="scan_interrupted",
    )

    assert row["scenario_passed"] is True
    assert row["repair_causes"] == ["scan_interrupted", "localization_drift"]
    assert row["wrong_repairs"] == 0


def test_ai_fault_case_cannot_pass_without_model_investigation():
    source = verified("robot_1", "r1")
    source.update(
        diagnosis={"cause": "scan_interrupted"},
        events=[{"kind": "repair", "data": {"operation": "restore_scan_forwarding"}}],
    )
    row = evaluate_fleet_case(
        "fault_recovery", "ai", plan(), [source, verified("robot_2", "r2")],
        expected_fault="scan_interrupted",
    )

    assert row["scenario_passed"] is False
    assert row["ai_fault_investigation_used"] is False


def test_handoff_preserves_source_failure_and_scores_separate_acceptance():
    source = verified("robot_1", "r1")
    source.update(
        status="unresolved",
        diagnosis={"cause": "scan_interrupted"},
        mission_acceptance=None,
        validation={"success": False},
        events=[{"kind": "tool", "title": "measure_topics"}],
    )
    handoff = verified("robot_2", "h1")
    row = evaluate_fleet_case(
        "handoff", "ai", plan(), [source, verified("robot_2", "r2")],
        expected_fault="scan_interrupted",
        handoff_run=handoff,
        handoff_record={"status": "verified"},
    )

    assert row["scenario_passed"] is True
    assert row["original_batch_restored"] is False
    assert row["handoff_verified"] is True
    assert row["unaffected_peer_completed"] is True


def test_summary_keeps_ai_and_rules_comparable():
    rows = []
    for mode in ("ai", "rules"):
        row = evaluate_fleet_case(
            "parallel", mode, plan(),
            [verified("robot_1", mode + "1"), verified("robot_2", mode + "2")],
            wall_seconds=5.0,
        )
        rows.append(row)

    summary = summarize_fleet_rows(rows)

    assert [(item["recovery_mode"], item["success_rate"]) for item in summary] == [
        ("ai", 1.0), ("rules", 1.0)
    ]


def test_wait_only_peer_uses_mission_acceptance_without_fake_pose_validation():
    source = verified("robot_1", "r1")
    source.update(
        diagnosis={"cause": "scan_interrupted"},
        events=[{"kind": "repair", "data": {
            "cause": "scan_interrupted", "operation": "restore_scan_forwarding",
        }}],
    )
    idle = verified("robot_2", "r2")
    idle.update(
        validation=None,
        steps=[{"action": "wait"}, {"action": "enter_idle"}],
    )

    row = evaluate_fleet_case(
        "fault_recovery", "rules", plan(), [source, idle],
        expected_fault="scan_interrupted",
    )

    assert row["independent_gazebo_acceptance"] is True
    assert row["scenario_passed"] is True
