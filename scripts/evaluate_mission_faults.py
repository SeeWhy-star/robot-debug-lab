"""Evaluate real faults triggered during an LLM-planned ordered mission."""

import argparse
import json
from pathlib import Path
import sys
import time

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robot_debug_lab.evaluation import evaluate_mission_run, summarize_mission_rows


FAULTS = ("scan_interrupted", "navigator_inactive", "controller_velocity_zero")
TERMINAL = {"verified", "unresolved", "failed", "canceled", "interrupted"}
PROMPT = "先去实体障碍箱旁边，然后回起点，最后去充电站等待。"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--mode", choices=["ai", "rules", "both"], default="ai")
    parser.add_argument("--fault", choices=[*FAULTS, "all"], default="all")
    parser.add_argument("--trials", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", default="docs/mission-fault-evaluation-results.json")
    args = parser.parse_args()
    modes = ("ai", "rules") if args.mode == "both" else (args.mode,)
    faults = FAULTS if args.fault == "all" else (args.fault,)
    output = Path(args.output)
    rows = []
    if args.resume and output.is_file():
        previous = json.loads(output.read_text())
        if previous.get("prompt") != PROMPT:
            raise SystemExit("已有评测文件使用了不同任务，不能续跑")
        rows = list(previous.get("rows") or [])

    with httpx.Client(base_url=args.url, timeout=220) as client:
        state = client.get("/api/state").raise_for_status().json()
        if state.get("simulation") != "nav2" or state.get("runtime") != "openfang":
            raise SystemExit("需要 OpenFang + Nav2/Gazebo 运行环境")
        headers = {"X-Lab-Token": state["token"]}
        for trial in range(1, args.trials + 1):
            expected_pairs = {(mode, fault) for mode in modes for fault in faults}
            existing_pairs = {
                (row["recovery_mode"], row["fault"])
                for row in rows if row.get("trial") == trial
            }
            if expected_pairs <= existing_pairs:
                print(f"trial {trial} 已完整，跳过")
                continue
            # A partial trial cannot be a controlled comparison unless every
            # row reuses one plan. Discard only that trial and regenerate it.
            rows = [row for row in rows if row.get("trial") != trial]
            planned = client.post(
                "/api/assistant/tasks",
                headers=headers,
                json={"prompt": PROMPT, "auto_repair": True, "execute": False},
            ).raise_for_status().json()
            plan = planned["plan"]
            if planned.get("run_ids") or not plan.get("steps"):
                raise SystemExit("复杂任务只规划请求意外执行或没有生成步骤")
            if plan.get("backend") == "deterministic":
                raise SystemExit("复杂任务错误绕过模型规划")
            for mode in modes:
                for fault in faults:
                    client.post(
                        "/api/robots/robot_1/reset", headers=headers, json={}
                    ).raise_for_status()
                    schedule = client.post(
                        "/api/robots/robot_1/fault-schedule",
                        headers=headers,
                        json={"kind": fault, "navigation_attempt": 2},
                    ).raise_for_status().json()
                    response = client.post(
                        f"/api/assistant/plans/{plan['id']}/runs",
                        headers=headers,
                        json={"recovery_mode": mode},
                    ).raise_for_status().json()
                    if not response.get("run_ids"):
                        raise SystemExit("已验证计划未生成任务运行")
                    run_id = response["run_ids"][0]
                    deadline = time.monotonic() + 210
                    while True:
                        run = client.get(f"/api/runs/{run_id}").raise_for_status().json()
                        if run["status"] in TERMINAL:
                            break
                        if time.monotonic() >= deadline:
                            raise SystemExit(
                                f"任务中途故障评测超时：{mode}/{fault}/{run_id}"
                            )
                        time.sleep(0.5)
                    row = evaluate_mission_run(run, fault, mode)
                    row.update(
                        trial=trial,
                        run_id=run_id,
                        plan_id=plan["id"],
                        plan_reused=True,
                        scheduled_navigation_attempt=schedule["navigation_attempt"],
                    )
                    rows.append(row)
                    print(json.dumps(row, ensure_ascii=False))

    result = {
        "generated_at": time.time(),
        "simulation": "gazebo_nav2",
        "prompt": PROMPT,
        "unique_plan_count": len({row["plan_id"] for row in rows}),
        "planning_usage_by_plan": {
            row["plan_id"]: {
                "prompt_tokens": row["planning_prompt_tokens"],
                "completion_tokens": row["planning_completion_tokens"],
            }
            for row in rows
        },
        "summary": summarize_mission_rows(rows),
        "rows": rows,
        "all_identified": all(row["identified_correctly"] for row in rows),
        "all_missions_restored": all(
            row["original_mission_restored"] for row in rows
        ),
        "all_preserved_prior_steps": all(
            bool(row["completed_steps_preserved"]) for row in rows
        ),
        "ai_has_no_rules_fallback": all(
            row["recovery_mode"] != "ai" or row["diagnostic_tool_calls"] > 0
            for row in rows
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if not all(
        result[key]
        for key in [
            "all_identified", "all_missions_restored",
            "all_preserved_prior_steps", "ai_has_no_rules_fallback",
        ]
    ):
        raise SystemExit("任务中途故障矩阵存在失败项，详情见 " + str(output))
    print("任务中途故障矩阵通过：" + str(output))


if __name__ == "__main__":
    main()
