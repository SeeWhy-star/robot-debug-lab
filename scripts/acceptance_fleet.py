"""Live acceptance for OpenFang fleet planning and parallel Nav2 execution."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import httpx


TERMINAL = {"verified", "unresolved", "failed", "canceled"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=180) as client:
        state = client.get("/api/state").raise_for_status().json()
        if len(state["robots"]) != 2:
            raise SystemExit("真实仿真中没有两台机器人")
        headers = {"X-Lab-Token": state["token"]}
        if args.reset:
            client.post(
                "/api/robots/robot_1/reset", headers=headers, json={}
            ).raise_for_status()
        response = client.post(
            "/api/fleet/tasks",
            headers=headers,
            json={
                "prompt": (
                    "根据两台机器人的实时位置安排任务：让更合适的一台去货架A，"
                    "另一台去充电站并等待5秒。"
                ),
                "auto_repair": True,
                "recovery_mode": "ai",
                "execute": True,
            },
        )
        response.raise_for_status()
        payload = response.json()
        plan = payload["plan"]
        tools = [item["name"] for item in plan.get("tool_trace", [])]
        if plan.get("backend") == "deterministic":
            raise SystemExit("车队任务没有调用模型")
        if tools != ["get_fleet_state", "resolve_destinations"]:
            raise SystemExit(f"车队Agent缺少现场或语义工具证据：{tools}")
        assignments = plan.get("assignments", [])
        if len(assignments) != 2 or len({a["robot_id"] for a in assignments}) != 2:
            raise SystemExit("车队计划没有为两台不同机器人生成子任务")
        if len(payload["run_ids"]) != 2:
            raise SystemExit("车队批次没有原子分派两项任务")

        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            runs = [
                client.get(f"/api/runs/{run_id}").raise_for_status().json()
                for run_id in payload["run_ids"]
            ]
            if all(run["status"] in TERMINAL for run in runs):
                break
            time.sleep(0.5)
        if any(run["status"] != "verified" for run in runs):
            raise SystemExit("并行任务没有全部通过：" + json.dumps(runs, ensure_ascii=False))
        final_state = client.get("/api/state").raise_for_status().json()
        if any(not robot.get("nav2_online") for robot in final_state["robots"]):
            raise SystemExit("任务完成后存在离线Nav2实例")

    result = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "runtime": state["runtime"],
        "plan_id": plan["id"],
        "planner_backend": plan["backend"],
        "planning_usage": plan.get("usage", {}),
        "tool_calls": tools,
        "assignments": [
            {
                "robot_id": item["robot_id"],
                "rationale": item["rationale"],
                "steps": [step["action"] for step in item["steps"]],
                "destinations": [
                    step.get("destination_id") for step in item["steps"]
                    if step.get("destination_id")
                ],
            }
            for item in assignments
        ],
        "runs": [
            {
                "run_id": run["id"],
                "robot_id": run["robot_id"],
                "status": run["status"],
                "completed_steps": run["mission_acceptance"]["completed_steps"],
                "total_steps": run["mission_acceptance"]["total_steps"],
                "final_gazebo_error": run["validation"]["error"],
            }
            for run in runs
        ],
        "nav2_online_after": {
            robot["id"]: robot["nav2_online"] for robot in final_state["robots"]
        },
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
