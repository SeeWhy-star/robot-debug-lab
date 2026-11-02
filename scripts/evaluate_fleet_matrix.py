"""Run repeatable two-robot planning, traffic, recovery and handoff experiments."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robot_debug_lab.fleet_evaluation import evaluate_fleet_case, summarize_fleet_rows


TERMINAL = {"verified", "unresolved", "failed", "canceled", "interrupted"}
SCENARIOS = {
    "parallel": {
        "prompt": (
            "保持以下明确分工且不要增加其他步骤："
            "Robot 01前往货架A；Robot 02前往充电站并等待2秒。"
        ),
        "auto_repair": True,
        "allow_handoff": False,
        "fault": None,
        "traffic_wait_timeout_seconds": 45.0,
        "expect_handoff": False,
    },
    "traffic": {
        "prompt": (
            "保持以下明确分工且不要增加其他步骤："
            "Robot 01从当前位置前往坐标(2.0, 0.5)，"
            "Robot 02从当前位置前往坐标(2.0, -0.5)。"
        ),
        "auto_repair": True,
        "allow_handoff": False,
        "fault": None,
        "traffic_wait_timeout_seconds": 45.0,
        "expect_handoff": False,
    },
    "traffic_timeout_handoff": {
        "prompt": (
            "保持以下明确分工且不要增加其他步骤："
            "Robot 01从当前位置前往坐标(2.0, 0.5)，"
            "Robot 02从当前位置前往坐标(2.0, -0.5)。"
            "如果路线等待超时，允许已完成任务的机器人接替。"
        ),
        "auto_repair": True,
        "allow_handoff": True,
        "fault": None,
        "traffic_wait_timeout_seconds": 3.0,
        "expect_handoff": True,
    },
    "fault_recovery": {
        "prompt": (
            "保持以下明确分工且不要增加其他步骤："
            "Robot 01前往实体障碍箱旁边；"
            "Robot 02原地等待2秒后进入空闲状态。"
        ),
        "auto_repair": True,
        "allow_handoff": False,
        "fault": "scan_interrupted",
        "traffic_wait_timeout_seconds": 45.0,
        "expect_handoff": False,
    },
    "lifecycle_recovery": {
        "prompt": (
            "保持以下明确分工且不要增加其他步骤："
            "Robot 01前往实体障碍箱旁边；"
            "Robot 02原地等待2秒后进入空闲状态。"
        ),
        "auto_repair": True,
        "allow_handoff": False,
        "fault": "navigator_inactive",
        "traffic_wait_timeout_seconds": 45.0,
        "expect_handoff": False,
    },
    "handoff": {
        "prompt": (
            "保持以下明确分工且不要增加其他步骤："
            "Robot 01前往实体障碍箱旁边；"
            "Robot 02原地等待2秒后进入空闲状态。"
            "如果其中一台无法完成，允许另一台接替未完成任务。"
        ),
        "auto_repair": False,
        "allow_handoff": True,
        "fault": "scan_interrupted",
        "traffic_wait_timeout_seconds": 45.0,
        "expect_handoff": True,
    },
}


def _write(path, rows, selected, modes, complete=False):
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "simulation": "gazebo_nav2_two_robot",
        "scenarios": list(selected),
        "modes": list(modes),
        "complete": complete,
        "unique_plan_count": len({row["plan_id"] for row in rows}),
        "planning_usage_by_plan": {
            row["plan_id"]: {
                "prompt_tokens": row["planning_prompt_tokens"],
                "completion_tokens": row["planning_completion_tokens"],
            }
            for row in rows
        },
        "summary": summarize_fleet_rows(rows),
        "rows": rows,
        "all_cases_passed": bool(rows) and all(row["scenario_passed"] for row in rows),
        "ai_fault_runs_used_model_tools": all(
            row["recovery_mode"] != "ai" or not row.get("fault")
            or row["ai_fault_investigation_used"]
            for row in rows
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def _wait_runs(client, run_ids, deadline):
    while time.monotonic() < deadline:
        runs = [
            client.get(f"/api/runs/{run_id}").raise_for_status().json()
            for run_id in run_ids
        ]
        if all(run.get("status") in TERMINAL for run in runs):
            return runs
        time.sleep(0.5)
    raise RuntimeError("车队原始任务等待超时：" + ",".join(run_ids))


def _wait_handoff(client, plan_id, source_ids, deadline):
    while time.monotonic() < deadline:
        plan = client.get(f"/api/assistant/plans/{plan_id}").raise_for_status().json()
        record = next(
            (
                item for item in reversed(plan.get("handoffs") or [])
                if item.get("source_run_id") in source_ids
            ),
            None,
        )
        if record:
            run = client.get(
                f"/api/runs/{record['handoff_run_id']}"
            ).raise_for_status().json()
            if run.get("status") in TERMINAL:
                # The workflow persists the final record immediately after the run.
                plan = client.get(
                    f"/api/assistant/plans/{plan_id}"
                ).raise_for_status().json()
                record = next(
                    item for item in reversed(plan.get("handoffs") or [])
                    if item.get("handoff_run_id") == run["id"]
                )
                return run, record
        time.sleep(0.5)
    raise RuntimeError("接替任务等待超时：" + plan_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    parser.add_argument("--mode", choices=["ai", "rules", "both"], default="both")
    parser.add_argument("--trials", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", default="docs/fleet-evaluation-results.json")
    args = parser.parse_args()
    selected = tuple(SCENARIOS) if args.scenario == "all" else (args.scenario,)
    modes = ("ai", "rules") if args.mode == "both" else (args.mode,)
    output = Path(args.output)
    rows = []
    if args.resume and output.is_file():
        previous = json.loads(output.read_text())
        if previous.get("scenarios") != list(selected) or previous.get("modes") != list(modes):
            raise SystemExit("已有评测文件的场景或模式不同，不能续跑")
        rows = list(previous.get("rows") or [])

    with httpx.Client(base_url=args.url, timeout=240) as client:
        state = client.get("/api/state").raise_for_status().json()
        if state.get("simulation") != "nav2" or state.get("runtime") != "openfang":
            raise SystemExit("需要 OpenFang + 双机器人 Nav2/Gazebo 运行环境")
        headers = {"X-Lab-Token": state["token"]}
        for trial in range(1, args.trials + 1):
            for scenario in selected:
                existing = {
                    row["recovery_mode"] for row in rows
                    if row.get("trial") == trial and row.get("scenario") == scenario
                }
                existing_rows = [
                    row for row in rows
                    if row.get("trial") == trial and row.get("scenario") == scenario
                ]
                if set(modes) <= existing and all(
                    row.get("scenario_passed") for row in existing_rows
                ):
                    print(f"trial {trial} / {scenario} 已完整，跳过")
                    continue
                # Controlled AI/rules comparisons must replay one identical plan.
                rows = [
                    row for row in rows
                    if not (row.get("trial") == trial and row.get("scenario") == scenario)
                ]
                spec = SCENARIOS[scenario]
                client.post(
                    "/api/robots/robot_1/reset", headers=headers, json={}
                ).raise_for_status()
                planning_attempts = 0
                planned = None
                for planning_attempts in (1, 2, 3):
                    response = client.post(
                        "/api/fleet/tasks",
                        headers=headers,
                        json={
                            "prompt": spec["prompt"],
                            "auto_repair": spec["auto_repair"],
                            "allow_handoff": spec["allow_handoff"],
                            "traffic_wait_timeout_seconds": spec[
                                "traffic_wait_timeout_seconds"
                            ],
                            "execute": False,
                        },
                    )
                    if response.is_success:
                        planned = response.json()
                        break
                    if response.status_code not in {409, 502} or planning_attempts == 3:
                        raise RuntimeError(
                            f"车队模型规划失败({response.status_code})：{response.text[:500]}"
                        )
                    print(
                        f"{scenario} 第{planning_attempts}次模型规划未通过网关，"
                        "使用同一提示进行有界重试",
                        flush=True,
                    )
                    time.sleep(1.0)
                plan = planned["plan"]
                if planned.get("run_ids") or plan.get("backend") != "openfang":
                    raise RuntimeError("车队计划意外执行或没有经过OpenFang模型")
                if {item["robot_id"] for item in plan.get("assignments", [])} != {
                    "robot_1", "robot_2"
                }:
                    raise RuntimeError("车队Agent没有保持受控实验的明确双机器人分工")
                for mode in modes:
                    client.post(
                        "/api/robots/robot_1/reset", headers=headers, json={}
                    ).raise_for_status()
                    schedule = None
                    if spec["fault"]:
                        schedule = client.post(
                            "/api/robots/robot_1/fault-schedule",
                            headers=headers,
                            json={"kind": spec["fault"], "navigation_attempt": 1},
                        ).raise_for_status().json()
                    started = time.monotonic()
                    dispatched = client.post(
                        f"/api/fleet/plans/{plan['id']}/runs",
                        headers=headers,
                        json={"recovery_mode": mode},
                    ).raise_for_status().json()
                    original = _wait_runs(
                        client, dispatched["run_ids"], time.monotonic() + 210
                    )
                    handoff_run = handoff_record = None
                    if spec["expect_handoff"]:
                        handoff_run, handoff_record = _wait_handoff(
                            client, plan["id"], set(dispatched["run_ids"]),
                            time.monotonic() + 210,
                        )
                    row = evaluate_fleet_case(
                        scenario,
                        mode,
                        plan,
                        original,
                        expected_fault=spec["fault"],
                        handoff_run=handoff_run,
                        handoff_record=handoff_record,
                        wall_seconds=time.monotonic() - started,
                    )
                    row.update(
                        trial=trial,
                        plan_reused=len(modes) > 1,
                        planning_attempts=planning_attempts,
                        scheduled_navigation_attempt=(
                            schedule.get("navigation_attempt") if schedule else None
                        ),
                    )
                    rows.append(row)
                    _write(output, rows, selected, modes, complete=False)
                    print(json.dumps(row, ensure_ascii=False))

    result = _write(output, rows, selected, modes, complete=True)
    if not result["all_cases_passed"] or not result["ai_fault_runs_used_model_tools"]:
        raise SystemExit("车队评测矩阵存在失败项，详情见 " + str(output))
    print("车队评测矩阵通过：" + str(output))


if __name__ == "__main__":
    main()
