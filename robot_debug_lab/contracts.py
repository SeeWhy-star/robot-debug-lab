"""Versioned public contract helpers for runs, evidence and evaluation."""

from __future__ import annotations

import time
from copy import deepcopy


CONTRACT_VERSION = "2.0"
PROMPT_VERSION_UNFROZEN = "legacy-diagnostic-unfrozen"
DIAGNOSTIC_PROMPT_VERSION = "diagnostic-structure-v1-frozen"

PHASES = {
    "planned", "navigating", "waiting_route", "executing", "investigating",
    "repairing", "replaying", "verifying", "verified",
    "nav2_ok_gazebo_failed", "failed", "escalated", "canceled",
}
TERMINAL_PHASES = {
    "verified", "nav2_ok_gazebo_failed", "failed", "escalated", "canceled"
}
ACTIVE_TARGET_PHASES = {"navigating", "waiting_route", "replaying"}

LEGACY_PHASES = {
    "queued": "planned",
    "navigating": "navigating",
    "reproducing": "navigating",
    "traffic_waiting": "waiting_route",
    "waiting": "executing",
    "simulated_charging": "executing",
    "idle": "executing",
    "investigating": "investigating",
    "replanning": "investigating",
    "repairing": "repairing",
    "validating": "replaying",
    "verified": "verified",
    "unresolved": "escalated",
    "diagnosed": "escalated",
    "failed": "failed",
    "interrupted": "failed",
    "canceled": "canceled",
}

_TRANSITIONS = {
    "planned": {"navigating", "executing"},
    "navigating": {"waiting_route", "executing", "investigating", "verifying"},
    "waiting_route": {"navigating", "replaying"},
    "executing": {"navigating", "verifying"},
    "investigating": {"repairing", "escalated", "navigating"},
    "repairing": {"replaying"},
    "replaying": {"waiting_route", "investigating", "verifying"},
    "verifying": {"verified", "nav2_ok_gazebo_failed"},
}

V2_EVENT_FIELDS = {
    "phase_changed": {"from", "to", "reason", "activity", "return_to"},
    "plan": {"plan_id", "plan_type", "planner_backend", "step_count", "summary"},
    "navigation_attempt": {
        "attempt", "stage", "target", "nav2_success", "nav2_status",
        "duration_seconds", "final_pose",
    },
    "route_wait": {
        "state", "reservation_token", "blocked_by", "wait_seconds",
        "timeout_seconds", "path_mode",
    },
    "agent_decision": {
        "agent", "mode", "round", "requested_tools", "reason_summary",
        "usage_delta",
    },
    "tool_call": {
        "tool", "actor", "input_summary", "output_summary", "evidence_id",
        "duration_ms", "success", "error",
    },
    "diagnosis": {
        "schema_version", "strategy", "classification", "known_cause",
        "suspected_subsystem", "summary", "cited_evidence", "ruled_out",
        "repair_requested", "requested_repair", "recommended_action",
        "cause", "evidence_ids", "repair_rationale",
    },
    "repair_authorization": {
        "schema_version", "decision", "known_cause", "requested_operation",
        "authorized_operation", "rule_version", "checked_at",
        "max_evidence_age_seconds", "evidence_ids", "checks", "reason",
    },
    "repair_execution": {
        "operation", "known_cause", "started_at", "finished_at", "success",
        "result_summary", "error",
    },
    "verification": {
        "scope", "nav2_success", "gazebo_success", "pose_source",
        "position_error_m", "threshold_m", "observed_at", "reason",
    },
    "escalation": {
        "reason", "suspected_subsystem", "summary", "cited_evidence",
        "recommended_action", "repair_executed",
    },
    "step_update": {"action", "state", "description", "failure_reason"},
    "warning": {"code", "message"},
    "error": {"code", "message", "retryable"},
    "cancellation": {"requested_by", "confirmed", "reason"},
}

DIAGNOSIS_STRATEGIES = {"rules", "no_tools_llm", "tools_llm"}
CLASSIFICATIONS = {"known_fault", "unknown_fault", "insufficient_evidence", "healthy"}
SUBSYSTEMS = {
    "sensor_pipeline", "tf", "localization", "nav2_lifecycle", "planner",
    "controller", "costmap", "qos_or_transport", "simulation", "unknown",
}
KNOWN_REPAIRS = {
    "scan_interrupted": "restore_scan_forwarding",
    "navigator_inactive": "resume_nav2_lifecycle",
    "controller_velocity_zero": "restore_controller_velocity_limits",
    "localization_drift": "reseed_amcl_from_gazebo_truth",
}
CAUSE_SUBSYSTEMS = {
    "scan_interrupted": "sensor_pipeline",
    "navigator_inactive": "nav2_lifecycle",
    "controller_velocity_zero": "controller",
    "localization_drift": "localization",
    "healthy": "unknown",
    "unknown": "unknown",
}
EVIDENCE_KINDS = {"tool_call", "navigation_attempt"}
TOOL_ACTORS = {
    "diagnostic_agent", "deterministic_rule", "repair_authorizer",
    "deterministic_preflight",
}


def phase_for(run):
    phase = (
        run.get("phase")
        if run.get("phase_managed")
        else LEGACY_PHASES.get(run.get("status"), run.get("phase", "failed"))
    )
    if phase not in PHASES:
        raise ValueError(f"Unknown run phase: {phase}")
    return phase


def phase_context(value=None, **updates):
    result = {
        "reason": None, "step_id": None, "activity": None, "return_to": None,
    }
    if isinstance(value, dict):
        result.update({key: value.get(key) for key in result})
    result.update({key: updates.get(key) for key in result if key in updates})
    return result


def public_run(run):
    """Return additive v2 fields without mutating the stored legacy body."""
    result = deepcopy(run)
    phase = phase_for(result)
    result["phase"] = phase
    result["phase_updated_at"] = result.get("phase_updated_at", result.get("created"))
    result["phase_context"] = phase_context(
        result.get("phase_context"),
        reason=("server_restarted" if result.get("status") == "interrupted" else
                (result.get("phase_context") or {}).get("reason")),
    )
    result["terminal"] = phase in TERMINAL_PHASES
    result.setdefault("finished", None)
    result.setdefault("active_target", None)
    result.setdefault("repair_authorization", None)
    result.setdefault("task_type", "navigation")
    if phase not in ACTIVE_TARGET_PHASES:
        result["active_target"] = None
    return result


def validate_transition(previous, following):
    if previous not in PHASES or following not in PHASES:
        raise ValueError("Unknown phase")
    if previous in TERMINAL_PHASES:
        raise ValueError("Terminal phase cannot transition")
    if following in {"failed", "canceled"}:
        return
    if following not in _TRANSITIONS.get(previous, set()):
        raise ValueError(f"Illegal phase transition: {previous} -> {following}")


def validate_event(kind, data):
    required = V2_EVENT_FIELDS.get(kind)
    if required is None:
        raise ValueError(f"Unknown v2 event kind: {kind}")
    if not isinstance(data, dict):
        raise ValueError("Event data must be an object")
    missing = required - set(data)
    if missing:
        raise ValueError(f"Missing event fields for {kind}: {sorted(missing)}")
    if kind == "tool_call" and data["actor"] not in TOOL_ACTORS:
        raise ValueError("Invalid tool actor")


def diagnosis_from_legacy(value, strategy):
    cause = value.get("cause", "unknown")
    if cause in KNOWN_REPAIRS:
        classification = "known_fault"
        known_cause = cause
        requested = KNOWN_REPAIRS[cause]
        repair_requested = True
    elif cause == "healthy":
        classification = "healthy"
        known_cause = requested = None
        repair_requested = False
    else:
        classification = "insufficient_evidence"
        known_cause = requested = None
        repair_requested = False
    citations = list(dict.fromkeys(value.get("evidence_ids") or []))
    result = {
        "schema_version": 2,
        "strategy": strategy,
        "classification": classification,
        "known_cause": known_cause,
        "suspected_subsystem": CAUSE_SUBSYSTEMS.get(cause, "unknown"),
        "summary": str(value.get("summary") or "证据不足，无法形成诊断。")[:4000],
        "cited_evidence": citations,
        "ruled_out": [],
        "repair_requested": repair_requested,
        "requested_repair": requested,
        "recommended_action": str(
            value.get("repair_rationale") or "升级人工调查。"
        )[:4000],
        # v1 aliases
        "cause": cause if cause in KNOWN_REPAIRS or cause == "healthy" else "unknown",
        "evidence_ids": citations,
        "repair_rationale": str(value.get("repair_rationale") or "证据不足。")[:4000],
    }
    validate_diagnosis(result)
    return result


def validate_diagnosis(value):
    validate_event("diagnosis", value)
    if value["strategy"] not in DIAGNOSIS_STRATEGIES:
        raise ValueError("Invalid diagnosis strategy")
    if value["classification"] not in CLASSIFICATIONS:
        raise ValueError("Invalid diagnosis classification")
    if value["suspected_subsystem"] not in SUBSYSTEMS:
        raise ValueError("Invalid suspected subsystem")
    known = value["classification"] == "known_fault"
    if known != (value["known_cause"] in KNOWN_REPAIRS):
        raise ValueError("known_cause does not match classification")
    if value["repair_requested"]:
        expected = KNOWN_REPAIRS.get(value["known_cause"])
        if not known or value["requested_repair"] != expected:
            raise ValueError("Requested repair is not the allowlisted mapping")
    elif value["requested_repair"] is not None:
        raise ValueError("Repair operation present without request")
    if value["classification"] in {"unknown_fault", "insufficient_evidence"} and value["repair_requested"]:
        raise ValueError("Unknown or insufficient diagnosis cannot request repair")
    return value


def validate_diagnosis_evidence(value, events, run_id, robot_id, diagnosed_at=None):
    """Validate citations by type, scope, actor and event time."""
    validate_diagnosis(value)
    by_id = {event.get("id"): event for event in events}
    citations = list(value["cited_evidence"])
    for ruled_out in value["ruled_out"]:
        citations.extend(ruled_out.get("evidence_ids") or [])
    for evidence_id in citations:
        event = by_id.get(evidence_id)
        if not event or event.get("kind") not in EVIDENCE_KINDS:
            raise ValueError("Diagnosis cited an ineligible evidence event")
        if event.get("run_id") != run_id or event.get("robot_id") != robot_id:
            raise ValueError("Diagnosis evidence scope mismatch")
        if diagnosed_at is not None and event.get("time", 0) > diagnosed_at:
            raise ValueError("Diagnosis cited future evidence")
        if value["strategy"] == "no_tools_llm" and event["kind"] != "navigation_attempt":
            raise ValueError("No-tools diagnosis cited tool evidence")
        if value["strategy"] == "rules" and event["kind"] == "tool_call":
            if (event.get("data") or {}).get("actor") != "deterministic_rule":
                raise ValueError("Rules diagnosis cited non-rule evidence")
    return value


def no_tools_input(robot_id, task_description, target, navigation_event):
    if navigation_event.get("kind") != "navigation_attempt":
        raise ValueError("No-tools input requires a navigation attempt")
    data = navigation_event["data"]
    return {
        "robot_id": robot_id,
        "task_description": task_description,
        "target": list(target),
        "navigation_attempt": {
            "evidence_id": navigation_event["id"],
            "nav2_success": data["nav2_success"],
            "nav2_status": data["nav2_status"],
            "duration_seconds": data["duration_seconds"],
            "final_pose": data["final_pose"],
        },
    }


def repair_authorization(diagnosis, observed_cause, evidence_ids, checked_at=None,
                         enabled=True):
    """Pure authorization decision; callers collect fresh server-side evidence."""
    validate_diagnosis(diagnosis)
    checked_at = checked_at or time.time()
    requested = diagnosis["requested_repair"]
    cause = diagnosis["known_cause"]
    decision = "not_applicable"
    authorized = None
    passed = False
    reason = "诊断未申请白名单修复"
    if not enabled:
        reason = "本次任务未授权自动修复"
    elif diagnosis["classification"] == "known_fault" and diagnosis["repair_requested"]:
        passed = observed_cause == cause and requested == KNOWN_REPAIRS.get(cause)
        if passed:
            decision, authorized = "authorized", requested
            reason = "新鲜服务器侧复测支持对应白名单修复"
        else:
            decision = "denied"
            reason = "新鲜服务器侧证据不支持请求的修复"
    result = {
        "schema_version": 2,
        "decision": decision,
        "known_cause": cause,
        "requested_operation": requested,
        "authorized_operation": authorized,
        "rule_version": "repair-allowlist-v1",
        "checked_at": checked_at,
        "max_evidence_age_seconds": 2.0,
        "evidence_ids": list(evidence_ids),
        "checks": [{
            "name": "fresh_mechanism_matches_requested_repair",
            "passed": passed,
            "expected": cause,
            "actual": observed_cause,
        }],
        "reason": reason,
    }
    validate_event("repair_authorization", result)
    return result


def validate_authorization_evidence(value, events, run_id, robot_id):
    """Authorization may depend only on a fresh server-side remeasurement."""
    validate_event("repair_authorization", value)
    by_id = {event.get("id"): event for event in events}
    if not value["evidence_ids"]:
        raise ValueError("Repair authorization requires fresh evidence")
    for evidence_id in value["evidence_ids"]:
        event = by_id.get(evidence_id)
        if (
            not event
            or event.get("kind") != "tool_call"
            or (event.get("data") or {}).get("actor") != "repair_authorizer"
        ):
            raise ValueError("Authorization evidence is not server-side remeasurement")
        if event.get("run_id") != run_id or event.get("robot_id") != robot_id:
            raise ValueError("Authorization evidence scope mismatch")
        age = value["checked_at"] - event.get("time", 0)
        if age < 0 or age > value["max_evidence_age_seconds"]:
            raise ValueError("Authorization evidence is stale")
    return value


def navigation_attempt_data(result, target, attempt, stage):
    pose = result.get("pose")
    nav2_success = bool(result.get("nav2_success", result.get("success", False)))
    return {
        "attempt": attempt,
        "stage": stage,
        "target": list(target),
        "nav2_success": nav2_success,
        "nav2_status": str(result.get("message", "unknown")),
        "duration_seconds": float(result.get("elapsed", 0.0)),
        "final_pose": (
            [pose.get("x"), pose.get("y")] if isinstance(pose, dict)
            and pose.get("x") is not None and pose.get("y") is not None else None
        ),
    }


def verification_data(result, scope="navigation"):
    pose = result.get("pose") or {}
    return {
        "scope": scope,
        "nav2_success": bool(result.get("nav2_success", result.get("success", False))),
        "gazebo_success": bool(result.get("success", False)),
        "pose_source": pose.get("source", "gazebo_odometry"),
        "position_error_m": result.get("error"),
        "threshold_m": 0.35,
        "observed_at": pose.get("observed", time.time()),
        "reason": None if result.get("success") else str(result.get("message", "verification_failed")),
    }
