"""Label-isolated aggregation for the v2 evaluation API."""

from collections import defaultdict
import time

from .contracts import (
    CONTRACT_VERSION,
    KNOWN_REPAIRS,
    PROMPT_VERSION_UNFROZEN,
    TERMINAL_PHASES,
    phase_for,
)


def _mean(values):
    return round(sum(values) / len(values), 3) if values else 0.0


def _events(run, kind):
    return [event for event in run.get("events", []) if event.get("kind") == kind]


def build_metrics(store, *, experiment_id=None, strategies=(), limit=100):
    labeled = store.labeled_runs(
        experiment_id=experiment_id, strategies=strategies, limit=limit
    )
    rows = [row for row in labeled if phase_for(row["run"]) in TERMINAL_PHASES]
    groups = defaultdict(list)
    for row in rows:
        run = row["run"]
        diagnosis = run.get("diagnosis") or {}
        strategy = diagnosis.get("strategy") or (
            "rules" if run.get("mode") == "rules" else "tools_llm"
        )
        groups[(
            strategy,
            run.get("model", "none" if strategy == "rules" else "unknown"),
            run.get("backend", "rules" if strategy == "rules" else "unknown"),
            run.get("prompt_version", PROMPT_VERSION_UNFROZEN),
        )].append(row)

    diagnosis_rows = []
    for (strategy, model, backend, prompt_version), group in sorted(groups.items()):
        known = [item for item in group if item["true_cause"] in KNOWN_REPAIRS]
        unseen = [item for item in group if item["true_cause"] not in KNOWN_REPAIRS]
        diagnoses = [(item["run"].get("diagnosis") or {}) for item in group]
        wrong_repairs = 0
        for item in group:
            expected = KNOWN_REPAIRS.get(item["true_cause"])
            for event in item["run"].get("events", []):
                if event.get("kind") not in {"repair_execution", "repair"}:
                    continue
                operation = (event.get("data") or {}).get("operation")
                wrong_repairs += operation != expected
        investigation_seconds = []
        for item in group:
            phase_events = _events(item["run"], "phase_changed")
            started = next((e["time"] for e in phase_events if e["data"]["to"] == "investigating"), None)
            ended = next((e["time"] for e in phase_events if started is not None and e["time"] >= started and e["data"]["to"] in TERMINAL_PHASES | {"repairing"}), None)
            if started is not None and ended is not None:
                investigation_seconds.append(max(0.0, ended - started))
        diagnosis_rows.append({
            "strategy": strategy,
            "model": model,
            "backend": backend,
            "prompt_version": prompt_version,
            "trials": len(group),
            "known_fault_accuracy": _mean([
                (item["run"].get("diagnosis") or {}).get("known_cause") == item["true_cause"]
                for item in known
            ]),
            "unknown_fault_classification_rate": _mean([
                (item["run"].get("diagnosis") or {}).get("classification") == "unknown_fault"
                for item in unseen
            ]),
            "unknown_subsystem_accuracy": _mean([
                (item["run"].get("diagnosis") or {}).get("suspected_subsystem") == item["true_subsystem"]
                for item in unseen
            ]),
            "insufficient_evidence_rate": _mean([
                d.get("classification") == "insufficient_evidence" for d in diagnoses
            ]),
            "escalation_rate": _mean([
                phase_for(item["run"]) == "escalated" for item in group
            ]),
            "repair_requests": sum(bool(d.get("repair_requested")) for d in diagnoses),
            "repairs_authorized": sum(
                (item["run"].get("repair_authorization") or {}).get("decision") == "authorized"
                for item in group
            ),
            "repairs_denied": sum(
                (item["run"].get("repair_authorization") or {}).get("decision") == "denied"
                for item in group
            ),
            "wrong_repairs": wrong_repairs,
            "average_tool_calls": _mean([
                len(_events(item["run"], "tool_call")) for item in group
            ]),
            "average_investigation_seconds": _mean(investigation_seconds),
            "average_total_tokens": _mean([
                int((item["run"].get("usage") or {}).get("prompt_tokens", 0))
                + int((item["run"].get("usage") or {}).get("completion_tokens", 0))
                for item in group
            ]),
        })

    phases = [phase_for(item["run"]) for item in rows]
    attempts = [
        event for item in rows for event in _events(item["run"], "navigation_attempt")
    ]
    verifications = [
        event for item in rows for event in _events(item["run"], "verification")
    ]
    experiments = sorted({item["experiment_id"] for item in rows})
    return {
        "contract_version": CONTRACT_VERSION,
        "server_time": time.time(),
        "sample": {
            "run_count": len(rows),
            "experiment_ids": experiments,
            "strategies": sorted({key[0] for key in groups}),
            "unlabeled_runs_excluded": store.unlabeled_run_count(),
            "small_sample_warning": any(len(group) < 5 for group in groups.values()),
            "notes": "每组样本量较少，结果仅描述本次受控实验",
        },
        "overall": {
            **{phase: phases.count(phase) for phase in TERMINAL_PHASES},
            "task_completion_rate": _mean([phase == "verified" for phase in phases]),
            "wrong_repairs": sum(item["wrong_repairs"] for item in diagnosis_rows),
        },
        "diagnosis_by_strategy": diagnosis_rows,
        "navigation": {
            "attempts": len(attempts),
            "nav2_success_rate": _mean([
                bool((event.get("data") or {}).get("nav2_success")) for event in attempts
            ]),
            "gazebo_verification_rate": _mean([
                bool((event.get("data") or {}).get("gazebo_success")) for event in verifications
            ]),
            "average_final_position_error_m": _mean([
                float(event["data"]["position_error_m"])
                for event in verifications
                if isinstance(event.get("data", {}).get("position_error_m"), (int, float))
            ]),
        },
        "traffic": {
            "reservations": 0, "conflict_waits": 0, "timeouts": 0,
            "total_wait_seconds": 0.0,
        },
    }
