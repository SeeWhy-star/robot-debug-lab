"""Metrics shared by live fault-matrix evaluation and tests."""

from collections import defaultdict


EXPECTED_REPAIRS = {
    "scan_interrupted": "restore_scan_forwarding",
    "navigator_inactive": "resume_nav2_lifecycle",
    "controller_velocity_zero": "restore_controller_velocity_limits",
}
DIAGNOSTIC_TOOLS = {
    "inspect_graph", "read_parameters", "sample_motion", "measure_topics"
}


def evaluate_run(run, expected_fault, mode, wall_seconds=None):
    repairs = [event for event in run.get("events", []) if event.get("kind") == "repair"]
    operations = [
        (event.get("data") or {}).get("operation")
        for event in repairs
        if isinstance(event.get("data"), dict)
    ]
    expected_operation = EXPECTED_REPAIRS[expected_fault]
    tools = [event for event in run.get("events", []) if event.get("kind") == "tool"]
    usage = run.get("usage") or {}
    return {
        "fault": expected_fault,
        "mode": mode,
        "identified_correctly": (run.get("diagnosis") or {}).get("cause") == expected_fault,
        "original_task_restored": run.get("status") == "verified"
        and bool((run.get("validation") or {}).get("success")),
        "wrong_repairs": sum(operation != expected_operation for operation in operations),
        "repair_operations": operations,
        "tool_calls": len(tools),
        "investigation_and_recovery_seconds": round(
            wall_seconds
            if wall_seconds is not None
            else max(0.0, run.get("finished", 0) - run.get("created", 0)),
            3,
        ),
        "prompt_tokens": int(usage.get("prompt_tokens", 0)),
        "completion_tokens": int(usage.get("completion_tokens", 0)),
        "status": run.get("status"),
    }


def evaluate_mission_run(run, expected_fault, recovery_mode, wall_seconds=None):
    """Score a fault that occurs after an ordered mission has started."""
    steps = run.get("steps") or []
    fault_steps = [step for step in steps if step.get("diagnosis")]
    diagnosed_step = next(
        (
            step for step in fault_steps
            if (step.get("diagnosis") or {}).get("cause") == expected_fault
        ),
        None,
    )
    repairs = [
        step.get("repair") for step in steps if isinstance(step.get("repair"), dict)
    ]
    expected_operation = EXPECTED_REPAIRS[expected_fault]
    operations = [repair.get("operation") for repair in repairs]
    completed_before_fault = []
    if diagnosed_step is not None:
        fault_index = steps.index(diagnosed_step)
        completed_before_fault = [
            step.get("id") for step in steps[:fault_index]
            if step.get("status") == "completed"
        ]
    planning_usage = run.get("planning_usage") or {}
    diagnosis_usage = run.get("usage") or {}
    recovery_seconds = wall_seconds
    if recovery_seconds is None and diagnosed_step is not None:
        recovery_seconds = max(
            0.0,
            diagnosed_step.get("finished", run.get("finished", 0))
            - diagnosed_step.get("started", run.get("created", 0)),
        )
    if recovery_seconds is None:
        recovery_seconds = max(0.0, run.get("finished", 0) - run.get("created", 0))
    return {
        "fault": expected_fault,
        "recovery_mode": recovery_mode,
        "identified_correctly": diagnosed_step is not None,
        "original_mission_restored": run.get("status") == "verified"
        and bool(steps)
        and all(step.get("status") == "completed" for step in steps),
        "completed_steps_preserved": completed_before_fault,
        "recovered_step_id": diagnosed_step.get("id") if diagnosed_step else None,
        "wrong_repairs": sum(operation != expected_operation for operation in operations),
        "repair_operations": operations,
        "diagnostic_tool_calls": sum(
            event.get("kind") == "tool" and event.get("title") in DIAGNOSTIC_TOOLS
            for event in run.get("events", [])
        ),
        "investigation_and_recovery_seconds": round(
            recovery_seconds,
            3,
        ),
        "planning_prompt_tokens": int(planning_usage.get("prompt_tokens", 0)),
        "planning_completion_tokens": int(planning_usage.get("completion_tokens", 0)),
        "diagnosis_prompt_tokens": int(diagnosis_usage.get("prompt_tokens", 0)),
        "diagnosis_completion_tokens": int(diagnosis_usage.get("completion_tokens", 0)),
        "status": run.get("status"),
    }


def summarize_mission_rows(rows):
    """Aggregate repeated mission-fault trials by recovery mode and mechanism."""
    groups = defaultdict(list)
    for row in rows:
        groups[(row["recovery_mode"], row["fault"])].append(row)
    summary = []
    for (mode, fault), group in sorted(groups.items()):
        count = len(group)
        summary.append({
            "recovery_mode": mode,
            "fault": fault,
            "trials": count,
            "identified_rate": round(
                sum(bool(row["identified_correctly"]) for row in group) / count, 3
            ),
            "mission_restore_rate": round(
                sum(bool(row["original_mission_restored"]) for row in group) / count,
                3,
            ),
            "wrong_repairs": sum(int(row["wrong_repairs"]) for row in group),
            "average_tool_calls": round(
                sum(int(row["diagnostic_tool_calls"]) for row in group) / count, 2
            ),
            "average_recovery_seconds": round(
                sum(float(row["investigation_and_recovery_seconds"]) for row in group)
                / count,
                3,
            ),
            "average_diagnosis_tokens": round(
                sum(
                    int(row["diagnosis_prompt_tokens"])
                    + int(row["diagnosis_completion_tokens"])
                    for row in group
                ) / count,
                2,
            ),
        })
    return summary
