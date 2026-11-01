"""Comparable metrics for repeated two-robot Gazebo/Nav2 experiments."""

from collections import defaultdict

from .evaluation import DIAGNOSTIC_TOOLS, EXPECTED_REPAIRS


def _usage_total(value):
    value = value or {}
    return int(value.get("prompt_tokens", 0)) + int(value.get("completion_tokens", 0))


def _events(runs, kind=None):
    events = [event for run in runs for event in run.get("events", [])]
    return events if kind is None else [event for event in events if event.get("kind") == kind]


def _reservations(runs):
    return [
        reservation
        for run in runs
        for step in run.get("steps", [])
        for reservation in step.get("traffic_reservations", [])
    ]


def _independently_accepted(run):
    navigates = any(
        step.get("action") in {"navigate", "return_to_start"}
        for step in run.get("steps", [])
    )
    if navigates:
        return bool((run.get("validation") or {}).get("success"))
    return bool((run.get("mission_acceptance") or {}).get("success"))


def evaluate_fleet_case(
    scenario,
    recovery_mode,
    plan,
    original_runs,
    *,
    expected_fault=None,
    handoff_run=None,
    handoff_record=None,
    wall_seconds=None,
):
    """Score one controlled fleet dispatch without trusting injection metadata."""
    originals = list(original_runs)
    accepted = [run for run in originals if run.get("status") == "verified"]
    if handoff_run and handoff_run.get("status") == "verified":
        accepted.append(handoff_run)
    evidence_runs = originals + ([handoff_run] if handoff_run else [])
    diagnoses = [
        run.get("diagnosis") for run in originals
        if isinstance(run.get("diagnosis"), dict)
    ] + [
        event.get("data")
        for event in _events(originals, "diagnosis")
        if isinstance(event.get("data"), dict)
    ]
    identified = (
        any(item.get("cause") == expected_fault for item in diagnoses)
        if expected_fault else None
    )
    repairs = _events(evidence_runs, "repair")
    operations = [
        (event.get("data") or {}).get("operation") for event in repairs
        if isinstance(event.get("data"), dict)
    ]
    expected_operation = EXPECTED_REPAIRS.get(expected_fault)
    repair_causes = [
        (event.get("data") or {}).get("cause") or expected_fault
        for event in repairs if isinstance(event.get("data"), dict)
    ]
    wrong_repairs = sum(
        operation != EXPECTED_REPAIRS.get(cause)
        for operation, cause in zip(operations, repair_causes)
    )
    reservations = _reservations(evidence_runs)
    waited = [item for item in reservations if item.get("blocked_by")]
    diagnostic_calls = sum(
        event.get("kind") == "tool" and event.get("title") in DIAGNOSTIC_TOOLS
        for event in _events(originals)
    )
    source = next((
        run for run in originals
        if (run.get("diagnosis") or {}).get("cause") == expected_fault or any(
            event.get("kind") == "diagnosis"
            and isinstance(event.get("data"), dict)
            and event["data"].get("cause") == expected_fault
            for event in run.get("events", [])
        )
    ), None) if expected_fault else None
    peers = [run for run in originals if source is None or run.get("id") != source.get("id")]
    all_original_verified = bool(originals) and all(
        run.get("status") == "verified"
        and bool((run.get("mission_acceptance") or {}).get("success"))
        for run in originals
    )
    handoff_verified = bool(
        handoff_run
        and handoff_run.get("status") == "verified"
        and (handoff_run.get("mission_acceptance") or {}).get("success")
        and handoff_record
        and handoff_record.get("status") == "verified"
    )
    independent_gazebo_acceptance = all(_independently_accepted(run) for run in accepted)
    ai_used_for_fault = (
        recovery_mode != "ai" or expected_fault is None or diagnostic_calls > 0
    )

    if scenario == "traffic":
        passed = all_original_verified and bool(waited) and all(
            item.get("path_mode") == "nav2_geometry" for item in reservations
        )
    elif scenario == "fault_recovery":
        passed = bool(
            all_original_verified and identified and expected_operation in operations
            and wrong_repairs == 0 and all(run.get("status") == "verified" for run in peers)
            and ai_used_for_fault
        )
    elif scenario == "handoff":
        passed = bool(
            source and source.get("status") in {"unresolved", "failed"}
            and identified and peers and all(run.get("status") == "verified" for run in peers)
            and not operations and handoff_verified and ai_used_for_fault
        )
    else:
        passed = all_original_verified
    passed = bool(passed and independent_gazebo_acceptance)

    planning_usage = plan.get("usage") or {}
    diagnosis_tokens = sum(_usage_total(run.get("usage")) for run in originals)
    elapsed = wall_seconds
    if elapsed is None:
        started = min((run.get("created", 0) for run in evidence_runs), default=0)
        finished = max((run.get("finished", started) for run in evidence_runs), default=started)
        elapsed = max(0.0, finished - started)
    return {
        "scenario": scenario,
        "recovery_mode": recovery_mode,
        "plan_id": plan.get("id"),
        "run_ids": [run.get("id") for run in originals],
        "handoff_run_id": handoff_run.get("id") if handoff_run else None,
        "statuses": {run.get("robot_id"): run.get("status") for run in originals},
        "scenario_passed": passed,
        "fault": expected_fault,
        "fault_identified": identified,
        "original_batch_restored": all_original_verified,
        "handoff_verified": handoff_verified,
        "unaffected_peer_completed": bool(peers) and all(
            run.get("status") == "verified" for run in peers
        ) if expected_fault else None,
        "independent_gazebo_acceptance": independent_gazebo_acceptance,
        "wrong_repairs": wrong_repairs,
        "repair_operations": operations,
        "repair_causes": repair_causes,
        "planner_tool_calls": len(plan.get("tool_trace") or []),
        "diagnostic_tool_calls": diagnostic_calls,
        "traffic_reservations": len(reservations),
        "traffic_conflict_waits": len(waited),
        "traffic_total_wait_seconds": round(
            sum(float(item.get("wait_seconds", 0)) for item in waited), 3
        ),
        "traffic_max_wait_seconds": round(
            max((float(item.get("wait_seconds", 0)) for item in waited), default=0.0), 3
        ),
        "traffic_global_fallbacks": sum(
            item.get("path_mode") == "global_fallback" for item in reservations
        ),
        "planning_prompt_tokens": int(planning_usage.get("prompt_tokens", 0)),
        "planning_completion_tokens": int(planning_usage.get("completion_tokens", 0)),
        "diagnosis_tokens": diagnosis_tokens,
        "ai_fault_investigation_used": ai_used_for_fault,
        "wall_seconds": round(float(elapsed), 3),
    }


def summarize_fleet_rows(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row["scenario"], row["recovery_mode"])].append(row)
    summary = []
    for (scenario, mode), group in sorted(groups.items()):
        count = len(group)
        summary.append({
            "scenario": scenario,
            "recovery_mode": mode,
            "trials": count,
            "success_rate": round(
                sum(bool(row["scenario_passed"]) for row in group) / count, 3
            ),
            "fault_identification_rate": (
                round(
                    sum(bool(row["fault_identified"]) for row in group) / count, 3
                ) if group[0].get("fault") else None
            ),
            "wrong_repairs": sum(int(row["wrong_repairs"]) for row in group),
            "average_wall_seconds": round(
                sum(float(row["wall_seconds"]) for row in group) / count, 3
            ),
            "average_traffic_wait_seconds": round(
                sum(float(row["traffic_total_wait_seconds"]) for row in group) / count,
                3,
            ),
            "average_diagnostic_tool_calls": round(
                sum(int(row["diagnostic_tool_calls"]) for row in group) / count, 2
            ),
            "average_diagnosis_tokens": round(
                sum(int(row["diagnosis_tokens"]) for row in group) / count, 2
            ),
        })
    return summary
