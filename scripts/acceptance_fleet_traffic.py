"""Live acceptance for Nav2-path fleet traffic reservations."""

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
    parser.add_argument("--output")
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=180) as client:
        state = client.get("/api/state").raise_for_status().json()
        headers = {"X-Lab-Token": state["token"]}
        client.post(
            "/api/robots/robot_1/reset", headers=headers, json={}
        ).raise_for_status()
        response = client.post(
            "/api/fleet/tasks",
            headers=headers,
            json={
                "prompt": (
                    "保持以下明确分工且不要增加其他步骤："
                    "Robot 01从当前位置前往坐标(2.0, 0.5)，"
                    "Robot 02从当前位置前往坐标(2.0, -0.5)。"
                ),
                "auto_repair": True,
                "recovery_mode": "ai",
                "execute": True,
                "allow_handoff": False,
            },
        )
        response.raise_for_status()
        payload = response.json()
        plan = payload["plan"]
        tools = [item["name"] for item in plan.get("tool_trace", [])]
        if "get_fleet_state" not in tools:
            raise SystemExit("车队Agent没有查询实时车队状态")
        assignments = {item["robot_id"]: item for item in plan["assignments"]}
        expected = {"robot_1": [2.0, 0.5], "robot_2": [2.0, -0.5]}
        if set(assignments) != set(expected):
            raise SystemExit("车队Agent没有保持两台机器人的明确分工")
        for robot_id, target in expected.items():
            steps = assignments[robot_id]["steps"]
            if len(steps) != 1 or [steps[0].get("x"), steps[0].get("y")] != target:
                raise SystemExit(f"{robot_id}没有保持明确坐标目标")

        deadline = time.monotonic() + 120
        runs = []
        while time.monotonic() < deadline:
            runs = [
                client.get(f"/api/runs/{run_id}").raise_for_status().json()
                for run_id in payload["run_ids"]
            ]
            if all(run["status"] in TERMINAL for run in runs):
                break
            time.sleep(0.5)
        if any(run["status"] != "verified" for run in runs):
            raise SystemExit("路线协调任务没有全部通过：" + json.dumps(runs, ensure_ascii=False))
        reservations = [
            reservation
            for run in runs
            for step in run["steps"]
            for reservation in step.get("traffic_reservations", [])
        ]
        waited = [item for item in reservations if item.get("blocked_by")]
        if len(reservations) != 2:
            raise SystemExit("两次导航没有都取得路线预约")
        if not waited:
            raise SystemExit("本次真实Nav2路线没有形成可验证的空间冲突")
        if any(item.get("path_mode") != "nav2_geometry" for item in reservations):
            raise SystemExit("路线预约没有使用真实Nav2路径几何")

        final_state = client.get("/api/state").raise_for_status().json()
        result = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "plan_id": plan["id"],
            "planning_usage": plan.get("usage", {}),
            "planner_tools": tools,
            "targets": expected,
            "reservations": reservations,
            "conflict_wait_count": len(waited),
            "max_wait_seconds": max(item["wait_seconds"] for item in waited),
            "runs": [
                {
                    "run_id": run["id"],
                    "robot_id": run["robot_id"],
                    "status": run["status"],
                    "final_gazebo_error": run["validation"]["error"],
                }
                for run in runs
            ],
            "nav2_online_after": {
                robot["id"]: robot["nav2_online"] for robot in final_state["robots"]
            },
        }
        client.post(
            "/api/robots/robot_1/reset", headers=headers, json={}
        ).raise_for_status()
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
