#!/usr/bin/env python3
"""Manually reproduce the unseen transform-update fault without an Agent."""

import argparse
import json
from pathlib import Path
import time

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument(
        "--output", default=".runtime/transform-fault-manual-reproduction.json"
    )
    args = parser.parse_args()
    if not 1 <= args.trials <= 5:
        raise SystemExit("--trials must be between 1 and 5")

    rows = []
    with httpx.Client(base_url=args.base_url, timeout=120) as client:
        state = client.get("/api/state")
        state.raise_for_status()
        token = state.json()["token"]
        headers = {"X-Lab-Token": token}
        for number in range(1, args.trials + 1):
            healthy = client.get("/api/robots/robot_1/transforms")
            healthy.raise_for_status()
            injected = client.post(
                "/api/robots/robot_1/fault",
                headers=headers,
                json={"kind": "transform_updates_stopped"},
            )
            injected.raise_for_status()
            failed_transform = client.get("/api/robots/robot_1/transforms")
            failed_transform.raise_for_status()
            started = time.monotonic()
            navigation = client.post(
                "/api/robots/robot_1/navigate",
                headers=headers,
                json={"x": 2.0, "y": 0.5},
            )
            elapsed = time.monotonic() - started
            navigation.raise_for_status()
            after = client.get("/api/robots/robot_1/transforms")
            after.raise_for_status()
            rows.append({
                "trial": number,
                "healthy_transform_state": healthy.json(),
                "fault_transform_state": failed_transform.json(),
                "navigation_result": navigation.json(),
                "wall_seconds": round(elapsed, 3),
                "post_navigation_transform_state": after.json(),
            })
            reset = client.post(
                "/api/robots/robot_1/reset", headers=headers, json={}
            )
            reset.raise_for_status()

    document = {
        "experiment": "manual-transform-update-stop-reproduction",
        "model": None,
        "backend": "manual_no_agent",
        "prompt_version": "diagnostic-structure-v1-frozen",
        "trials_requested": args.trials,
        "target": [2.0, 0.5],
        "agent_used": False,
        "diagnosis_used": False,
        "created": time.time(),
        "trials": rows,
    }
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(document, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
