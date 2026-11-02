"""Live acceptance for map-grounded fleet spatial analysis and execution."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import httpx


TERMINAL = {"verified", "unresolved", "failed", "canceled"}
PROMPTS = {
    "maximize_separation": "两个机器人找到相互之间的最远距离",
    "balanced_coverage": "让两台机器人分散到合适的安全位置，尽量均衡覆盖仓库，停好即可",
    "rendezvous": "让两台机器人到合适的位置安全会合，彼此保持安全距离，停好即可",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--output")
    parser.add_argument("--objective", choices=sorted(PROMPTS), default="maximize_separation")
    parser.add_argument("--no-reset-after", action="store_true")
    args = parser.parse_args()
    started = time.monotonic()
    with httpx.Client(base_url=args.url, timeout=180) as client:
        state = client.get("/api/state").raise_for_status().json()
        if len(state["robots"]) != 2:
            raise SystemExit("真实仿真中没有两台机器人")
        headers = {"X-Lab-Token": state["token"]}
        client.post(
            "/api/robots/robot_1/reset", headers=headers, json={}
        ).raise_for_status()
        initial = client.get("/api/state").raise_for_status().json()
        response = client.post(
            "/api/fleet/tasks",
            headers=headers,
            json={
                "prompt": PROMPTS[args.objective],
                "auto_repair": True,
                "recovery_mode": "ai",
                "execute": True,
                "allow_handoff": True,
            },
        ).raise_for_status()
        payload = response.json()
        plan = payload["plan"]
        tools = [item["name"] for item in plan.get("tool_trace", [])]
        if tools != ["get_fleet_state", "analyze_fleet_poses"]:
            raise SystemExit(f"空间任务缺少实时状态或地图分析证据：{tools}")
        analysis = plan.get("spatial_analysis") or {}
        if not analysis.get("resolved"):
            raise SystemExit("空间分析没有产生安全可达的双机器人站位")
        if analysis.get("objective") != args.objective:
            raise SystemExit("Agent没有选择对应的空间目标")
        if plan.get("allow_handoff") is not False:
            raise SystemExit("成对空间目标不应允许单机器人接替")
        expected = {
            item["robot_id"]: [item["x"], item["y"]]
            for item in analysis["assignments"]
        }
        assigned = {
            item["robot_id"]: [
                item["steps"][0]["x"], item["steps"][0]["y"]
            ]
            for item in plan["assignments"]
        }
        if assigned != expected:
            raise SystemExit("Agent改变了空间工具绑定的机器人目标坐标")
        if len(payload["run_ids"]) != 2:
            raise SystemExit("空间任务没有原子分派给两台机器人")

        deadline = time.monotonic() + 150
        acceptance = None
        while time.monotonic() < deadline:
            runs = [
                client.get(f"/api/runs/{run_id}").raise_for_status().json()
                for run_id in payload["run_ids"]
            ]
            saved_plan = client.get(
                f"/api/assistant/plans/{plan['id']}"
            ).raise_for_status().json()
            acceptance = saved_plan.get("fleet_acceptance")
            if acceptance:
                break
            if all(run["status"] in TERMINAL for run in runs):
                time.sleep(0.2)
            else:
                time.sleep(0.5)
        if any(run["status"] != "verified" for run in runs):
            raise SystemExit("空间任务没有全部通过：" + json.dumps(runs, ensure_ascii=False))
        if not acceptance or not acceptance.get("success"):
            raise SystemExit("缺少通过的Gazebo双机器人空间验收")
        final_state = client.get("/api/state").raise_for_status().json()

        result = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "plan_id": plan["id"],
            "prompt": PROMPTS[args.objective],
            "planner_backend": plan["backend"],
            "planning_usage": plan.get("usage", {}),
            "tool_calls": tools,
            "initial_poses": {
                item["id"]: item["pose"] for item in initial["robots"]
            },
            "analysis": analysis,
            "assigned_targets": assigned,
            "runs": [
                {
                    "run_id": run["id"],
                    "robot_id": run["robot_id"],
                    "status": run["status"],
                    "final_gazebo_error": run["validation"]["error"],
                }
                for run in runs
            ],
            "fleet_acceptance": acceptance,
            "final_poses": {
                item["id"]: item["pose"] for item in final_state["robots"]
            },
            "nav2_online_after": {
                item["id"]: item["nav2_online"] for item in final_state["robots"]
            },
        }
        if not args.no_reset_after:
            client.post(
                "/api/robots/robot_1/reset", headers=headers, json={}
            ).raise_for_status()

    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
