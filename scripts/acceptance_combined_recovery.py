"""Live acceptance for system repair followed by semantic route recovery."""

import argparse
import json
from pathlib import Path
import time

import httpx


FAULTS = ("scan_interrupted", "navigator_inactive", "controller_velocity_zero")
EXPECTED_REPAIRS = {
    "scan_interrupted": "restore_scan_forwarding",
    "navigator_inactive": "resume_nav2_lifecycle",
    "controller_velocity_zero": "restore_controller_velocity_limits",
}
TERMINAL = {"verified", "unresolved", "failed", "canceled", "interrupted"}
PROMPT = "先去充电站，然后去货架 A，最后在原地等待。"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--fault", choices=FAULTS, default="navigator_inactive")
    parser.add_argument("--output", default="docs/combined-recovery-results.json")
    args = parser.parse_args()

    with httpx.Client(base_url=args.url, timeout=260) as client:
        state = client.get("/api/state").raise_for_status().json()
        if state.get("simulation") != "nav2" or state.get("runtime") != "openfang":
            raise SystemExit("需要 OpenFang + Nav2/Gazebo 运行环境")
        headers = {"X-Lab-Token": state["token"]}
        client.post(
            "/api/robots/robot_1/reset", headers=headers, json={}
        ).raise_for_status()
        scenario = client.post(
            "/api/robots/robot_1/scenario",
            headers=headers,
            json={"kind": "shelf_a_primary_blocked"},
        ).raise_for_status().json()
        schedule = client.post(
            "/api/robots/robot_1/fault-schedule",
            headers=headers,
            json={"kind": args.fault, "navigation_attempt": 2},
        ).raise_for_status().json()
        response = client.post(
            "/api/assistant/tasks",
            headers=headers,
            json={"prompt": PROMPT, "auto_repair": True, "recovery_mode": "ai"},
        ).raise_for_status().json()
        if not response.get("run_ids"):
            raise SystemExit("复杂任务没有生成可执行运行")
        run_id = response["run_ids"][0]
        deadline = time.monotonic() + 250
        while True:
            run = client.get(f"/api/runs/{run_id}").raise_for_status().json()
            if run["status"] in TERMINAL:
                break
            if time.monotonic() >= deadline:
                raise SystemExit(f"组合恢复验收超时：{run_id}")
            time.sleep(0.5)

    shelf_steps = [
        step for step in run.get("steps", [])
        if step.get("destination_id") == "shelf_a"
    ]
    if len(shelf_steps) != 1:
        raise SystemExit("模型计划必须包含且只包含一个货架 A 导航步骤")
    step = shelf_steps[0]
    attempts = step.get("attempts") or []
    tool_names = [
        event["title"] for event in run.get("events", [])
        if event.get("kind") == "tool"
    ]
    result = {
        "generated_at": time.time(),
        "run_id": run_id,
        "plan_id": response["plan"]["id"],
        "prompt": PROMPT,
        "fault": args.fault,
        "scenario": scenario,
        "scheduled_navigation_attempt": schedule["navigation_attempt"],
        "status": run["status"],
        "system_diagnosis": step.get("diagnosis"),
        "system_repair": step.get("repair"),
        "post_repair_preflight": step.get("post_repair_preflight"),
        "selected_approach": step.get("approach_id"),
        "attempts": [
            {
                "phase": attempt.get("phase"),
                "approach_id": attempt.get("approach_id"),
                "success": (attempt.get("result") or {}).get("success"),
                "error": (attempt.get("result") or {}).get("error"),
                "message": (attempt.get("result") or {}).get("message"),
            }
            for attempt in attempts
        ],
        "plan_version": run.get("plan_version"),
        "replan_count": len(run.get("plan_revisions") or []),
        "completed_steps": [
            step["id"] for step in run.get("steps", [])
            if step.get("status") == "completed"
        ],
        "tool_calls": tool_names,
        "usage": run.get("usage"),
        "acceptance": run.get("mission_acceptance"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    checks = [
        run["status"] == "verified",
        all(item.get("status") == "completed" for item in run.get("steps", [])),
        (step.get("diagnosis") or {}).get("cause") == args.fault,
        (step.get("repair") or {}).get("operation") == EXPECTED_REPAIRS[args.fault],
        (step.get("post_repair_validation") or {}).get("success") is False,
        (step.get("post_repair_preflight") or {}).get("cause") == "healthy",
        step.get("route_recovery_ready") is True,
        step.get("approach_id") == "southwest",
        [attempt.get("phase") for attempt in attempts]
        == ["initial", "post_repair_replay", "replanned"],
        [attempt.get("result", {}).get("success") for attempt in attempts]
        == [False, False, True],
        run.get("plan_version") == 2,
        len(run.get("plan_revisions") or []) == 1,
        "get_mission_state" in tool_names,
        "list_approach_options" in tool_names,
        any(
            event.get("kind") == "failure_transition"
            for event in run.get("events", [])
        ),
    ]
    if not all(checks):
        raise SystemExit("系统故障与路线阻塞组合恢复验收失败")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
