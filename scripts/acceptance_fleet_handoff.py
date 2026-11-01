"""Live acceptance for fault-isolated fleet task handoff."""

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
        client.post(
            "/api/robots/robot_1/fault-schedule",
            headers=headers,
            json={"kind": "scan_interrupted", "navigation_attempt": 1},
        ).raise_for_status()
        response = client.post(
            "/api/fleet/tasks",
            headers=headers,
            json={
                "prompt": (
                    "Robot 01去货架A，Robot 02去充电站并等待5秒。"
                    "如果其中一台无法完成，允许另一台接替未完成任务。"
                ),
                "auto_repair": False,
                "recovery_mode": "rules",
                "execute": True,
                "allow_handoff": True,
            },
        )
        response.raise_for_status()
        payload = response.json()
        plan = payload["plan"]
        assignment = {item["robot_id"]: item for item in plan["assignments"]}
        if set(assignment) != {"robot_1", "robot_2"}:
            raise SystemExit("车队Agent没有保持用户明确指定的机器人分工")
        if assignment["robot_1"]["steps"][0].get("destination_id") != "shelf_a":
            raise SystemExit("Robot 01任务没有绑定货架A语义目标")

        deadline = time.monotonic() + 150
        original_runs = []
        handoff = None
        handoff_record = None
        while time.monotonic() < deadline:
            original_runs = [
                client.get(f"/api/runs/{run_id}").raise_for_status().json()
                for run_id in payload["run_ids"]
            ]
            current_plan = client.get(
                f"/api/assistant/plans/{plan['id']}"
            ).raise_for_status().json()
            if current_plan.get("handoffs"):
                handoff_record = current_plan["handoffs"][0]
                handoff = client.get(
                    f"/api/runs/{handoff_record['handoff_run_id']}"
                ).raise_for_status().json()
                if handoff["status"] in TERMINAL:
                    break
            time.sleep(0.5)
        by_robot = {run["robot_id"]: run for run in original_runs}
        source = by_robot["robot_1"]
        peer = by_robot["robot_2"]
        if source["status"] != "unresolved":
            raise SystemExit(f"Robot 01故障任务没有保留为unresolved：{source['status']}")
        if (source.get("diagnosis") or {}).get("cause") != "scan_interrupted":
            raise SystemExit("Robot 01没有从现场证据识别激光链路中断")
        if peer["status"] != "verified":
            raise SystemExit("Robot 02没有先完成自己的原始任务")
        if not handoff_record or not handoff or handoff["status"] != "verified":
            raise SystemExit("未完成任务没有被健康机器人接替并验收")
        if handoff["robot_id"] != "robot_2" or handoff["handoff_from"]["robot_id"] != "robot_1":
            raise SystemExit("接替来源或目标机器人错误")

        final_state = client.get("/api/state").raise_for_status().json()
        result = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "plan_id": plan["id"],
            "planning_usage": plan.get("usage", {}),
            "planner_tools": [item["name"] for item in plan.get("tool_trace", [])],
            "scheduled_fault_hidden_from_agent": "scan_interrupted on robot_1 navigation attempt 1",
            "source_run": {
                "run_id": source["id"],
                "robot_id": source["robot_id"],
                "status": source["status"],
                "diagnosis": source["diagnosis"]["cause"],
                "auto_repair": False,
            },
            "peer_original_run": {
                "run_id": peer["id"],
                "robot_id": peer["robot_id"],
                "status": peer["status"],
                "completed_steps": peer["mission_acceptance"]["completed_steps"],
            },
            "handoff": {
                **handoff_record,
                "status": handoff["status"],
                "completed_steps": handoff["mission_acceptance"]["completed_steps"],
                "final_gazebo_error": handoff["validation"]["error"],
                "model_tokens": sum(handoff["usage"].values()),
            },
            "nav2_online_after": {
                robot["id"]: robot["nav2_online"] for robot in final_state["robots"]
            },
            "scan_after_handoff_before_cleanup": {
                robot["id"]: robot.get("scan") for robot in final_state["robots"]
            },
        }
        # The source fault is intentionally left unrepaired during the handoff
        # assertion. Restore the shared world only after recording isolation.
        client.post(
            "/api/robots/robot_1/reset", headers=headers, json={}
        ).raise_for_status()
        cleanup = None
        for _ in range(60):
            cleanup_state = client.get("/api/state").raise_for_status().json()
            cleanup = {
                robot["id"]: {
                    "nav2_online": robot["nav2_online"],
                    "nav2_scan_hz": (robot.get("scan") or {}).get("nav2_scan_hz"),
                }
                for robot in cleanup_state["robots"]
            }
            if all(
                item["nav2_online"] and (item["nav2_scan_hz"] or 0) > 1
                for item in cleanup.values()
            ):
                break
            time.sleep(0.5)
        result["cleanup"] = cleanup
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
