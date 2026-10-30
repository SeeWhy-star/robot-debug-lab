"""Live Gazebo/Nav2 acceptance for evidence-grounded approach replanning."""

import argparse
import json
from pathlib import Path
import time

import httpx


TERMINAL = {"verified", "unresolved", "failed", "canceled", "interrupted"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--prompt", default="去货架 A")
    parser.add_argument("--output", default="docs/replanning-acceptance-results.json")
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=65) as client:
        state = client.get("/api/state").raise_for_status().json()
        if state.get("simulation") != "nav2" or state.get("runtime") != "openfang":
            raise SystemExit("需要 OpenFang + Nav2/Gazebo 运行环境")
        headers = {"X-Lab-Token": state["token"]}
        client.post("/api/robots/robot_1/reset", headers=headers, json={}).raise_for_status()
        scenario = client.post(
            "/api/robots/robot_1/scenario", headers=headers,
            json={"kind": "shelf_a_primary_blocked"},
        ).raise_for_status().json()
        response = client.post(
            "/api/assistant/tasks", headers=headers,
            json={"prompt": args.prompt, "auto_repair": True},
        ).raise_for_status().json()
        run_id = response["run_ids"][0]
        deadline = time.monotonic() + 180
        while True:
            run = client.get(f"/api/runs/{run_id}").raise_for_status().json()
            if run["status"] in TERMINAL:
                break
            if time.monotonic() >= deadline:
                raise SystemExit(f"重规划验收超时：{run_id}")
            time.sleep(0.5)
    revisions = run.get("plan_revisions") or []
    step = run.get("steps", [{}])[-1]
    attempts = step.get("attempts") or []
    tool_names = [
        event["title"] for event in run.get("events", [])
        if event.get("kind") == "tool"
    ]
    result = {
        "run_id": run_id,
        "scenario": scenario,
        "status": run["status"],
        "plan_version": run.get("plan_version"),
        "replan_count": len(revisions),
        "selected_approach": step.get("approach_id"),
        "attempts": [
            {
                "approach_id": attempt.get("approach_id"),
                "success": (attempt.get("result") or {}).get("success"),
                "error": (attempt.get("result") or {}).get("error"),
            }
            for attempt in attempts
        ],
        "tool_calls": tool_names,
        "usage": run.get("usage"),
        "acceptance": run.get("mission_acceptance"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    checks = [
        run["status"] == "verified",
        run.get("plan_version") == 2,
        len(revisions) == 1,
        step.get("approach_id") == "southwest",
        len(attempts) == 2,
        attempts[0]["result"]["success"] is False,
        attempts[1]["result"]["success"] is True,
        "get_mission_state" in tool_names,
        "list_approach_options" in tool_names,
    ]
    if not all(checks):
        raise SystemExit("任务级重规划验收失败")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
