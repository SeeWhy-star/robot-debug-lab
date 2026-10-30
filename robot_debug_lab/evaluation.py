"""Metrics shared by live fault-matrix evaluation and tests."""


EXPECTED_REPAIRS = {
    "scan_interrupted": "restore_scan_forwarding",
    "navigator_inactive": "resume_nav2_lifecycle",
    "controller_velocity_zero": "restore_controller_velocity_limits",
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
