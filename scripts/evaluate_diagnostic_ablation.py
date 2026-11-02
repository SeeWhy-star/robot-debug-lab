#!/usr/bin/env python3
"""Run a small, label-isolated ablation on the unseen transform fault."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from robot_debug_lab.contracts import DIAGNOSTIC_PROMPT_VERSION


STRATEGIES = (
    ("rules", "rules"),
    ("no_tools_llm", "no_tools_llm"),
    ("tools_llm", "ai"),
)
TRUE_CAUSE = "transform_updates_stopped"
TRUE_SUBSYSTEM = "tf"
TASK = "排查机器人无法到达目标的原因，逐步收集现场证据并给出结论。"


def _write(path, document):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _label(database, run_id, experiment_id):
    """Write private truth without constructing Store against a live service."""
    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute(
            """INSERT OR REPLACE INTO experiment_labels
               (run_id, experiment_id, true_cause, true_subsystem, created)
               VALUES (?, ?, ?, ?, ?)""",
            (run_id, experiment_id, TRUE_CAUSE, TRUE_SUBSYSTEM, time.time()),
        )
        connection.commit()


def _wait_for_run(client, run_id, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}")
        response.raise_for_status()
        last = response.json()
        if last.get("terminal"):
            return last
        time.sleep(0.5)
    raise TimeoutError(
        f"run {run_id} did not finish within {timeout_seconds}s; "
        f"last phase={None if last is None else last.get('phase')}"
    )


def _summarize(run, wall_seconds):
    events = run.get("events") or []
    diagnosis = run.get("diagnosis") or {}
    return {
        "run_id": run["id"],
        "phase": run.get("phase"),
        "status": run.get("status"),
        "model": run.get("model"),
        "backend": run.get("backend"),
        "prompt_version": run.get("prompt_version"),
        "wall_seconds": round(wall_seconds, 3),
        "diagnosis": {
            "strategy": diagnosis.get("strategy"),
            "classification": diagnosis.get("classification"),
            "known_cause": diagnosis.get("known_cause"),
            "suspected_subsystem": diagnosis.get("suspected_subsystem"),
            "summary": diagnosis.get("summary"),
            "cited_evidence": diagnosis.get("cited_evidence"),
            "repair_requested": diagnosis.get("repair_requested"),
            "recommended_action": diagnosis.get("recommended_action"),
        },
        "repair_authorization": run.get("repair_authorization"),
        "usage": run.get("usage"),
        "tool_calls": sum(event.get("kind") == "tool_call" for event in events),
        "navigation_attempts": sum(
            event.get("kind") == "navigation_attempt" for event in events
        ),
        "event_count": len(events),
        "error": run.get("error"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--trials-per-strategy", type=int, default=2)
    parser.add_argument("--run-timeout", type=float, default=240.0)
    parser.add_argument("--database", default=".runtime/lab.sqlite3")
    parser.add_argument(
        "--output", default=".runtime/unseen-transform-ablation.json"
    )
    parser.add_argument(
        "--experiment-id",
        default=f"unseen-transform-{time.strftime('%Y%m%d-%H%M%S')}",
    )
    args = parser.parse_args()
    if not 1 <= args.trials_per_strategy <= 5:
        raise SystemExit("--trials-per-strategy must be between 1 and 5")
    if args.run_timeout <= 0:
        raise SystemExit("--run-timeout must be positive")

    document = {
        "experiment_id": args.experiment_id,
        "experiment": "unseen-transform-diagnostic-ablation",
        "created": time.time(),
        "prompt_version": DIAGNOSTIC_PROMPT_VERSION,
        "true_cause_location": "private experiment_labels table only",
        "fault_mechanism": (
            "AMCL remains active but its real tf_broadcast parameter is disabled"
        ),
        "target": [2.0, 0.5],
        "task": TASK,
        "trials_per_strategy": args.trials_per_strategy,
        "strategies": [name for name, _ in STRATEGIES],
        "server": {},
        "trials": [],
        "metrics": None,
        "completed": False,
    }
    _write(args.output, document)

    with httpx.Client(base_url=args.base_url, timeout=180) as client:
        state_response = client.get("/api/state")
        state_response.raise_for_status()
        state = state_response.json()
        token = state["token"]
        headers = {"X-Lab-Token": token}
        document["server"] = {
            "contract_version": state.get("contract_version"),
            "model": state.get("model"),
            "backend": state.get("runtime"),
            "simulation": state.get("simulation"),
        }
        _write(args.output, document)

        try:
            for strategy, mode in STRATEGIES:
                for repetition in range(1, args.trials_per_strategy + 1):
                    row = {
                        "strategy": strategy,
                        "repetition": repetition,
                        "result": None,
                        "error": None,
                    }
                    try:
                        reset = client.post(
                            "/api/robots/robot_1/reset", headers=headers, json={}
                        )
                        reset.raise_for_status()
                        healthy = client.get("/api/robots/robot_1/transforms")
                        healthy.raise_for_status()
                        injected = client.post(
                            "/api/robots/robot_1/fault",
                            headers=headers,
                            json={"kind": TRUE_CAUSE},
                        )
                        injected.raise_for_status()
                        fault_state = client.get(
                            "/api/robots/robot_1/transforms"
                        )
                        fault_state.raise_for_status()
                        row["precondition"] = {
                            "healthy_all_available": healthy.json().get(
                                "all_available"
                            ),
                            "fault_all_available": fault_state.json().get(
                                "all_available"
                            ),
                            "fault_transforms": fault_state.json(),
                        }
                        started = time.monotonic()
                        response = client.post(
                            "/api/runs",
                            headers=headers,
                            json={
                                "robots": ["robot_1"],
                                "mode": mode,
                                "prompt": TASK,
                                "auto_repair": True,
                            },
                        )
                        response.raise_for_status()
                        run_id = response.json()["run_ids"][0]
                        run = _wait_for_run(client, run_id, args.run_timeout)
                        row["result"] = _summarize(
                            run, time.monotonic() - started
                        )
                        _label(
                            args.database, run_id, args.experiment_id
                        )
                    except Exception as error:  # Preserve failed trials honestly.
                        row["error"] = f"{type(error).__name__}: {error}"
                    document["trials"].append(row)
                    _write(args.output, document)
                    print(
                        f"{strategy} trial {repetition}: "
                        f"{row['error'] or row['result']['phase']}",
                        flush=True,
                    )

            metrics = client.get(
                "/api/metrics",
                params={"experiment_id": args.experiment_id, "limit": 100},
            )
            metrics.raise_for_status()
            document["metrics"] = metrics.json()
            document["completed"] = all(
                row["result"] is not None and row["error"] is None
                for row in document["trials"]
            )
        finally:
            try:
                client.post(
                    "/api/robots/robot_1/reset", headers=headers, json={}
                ).raise_for_status()
            except Exception as error:
                document["final_reset_error"] = (
                    f"{type(error).__name__}: {error}"
                )
            document["finished"] = time.time()
            _write(args.output, document)

    print(json.dumps(document, ensure_ascii=False, indent=2))
    if not document["completed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
