"""Run each real Nav2 fault against AI and deterministic rule baselines."""

import argparse
import json
from pathlib import Path
import time

import httpx

from robot_debug_lab.evaluation import evaluate_run


FAULTS = ("scan_interrupted", "navigator_inactive", "controller_velocity_zero")
TERMINAL = {"verified", "unresolved", "failed", "canceled", "diagnosed", "interrupted"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--mode", choices=["ai", "rules", "both"], default="both")
    parser.add_argument("--fault", choices=[*FAULTS, "all"], default="all")
    parser.add_argument("--output", default="docs/fault-evaluation-results.json")
    args = parser.parse_args()
    modes = ("ai", "rules") if args.mode == "both" else (args.mode,)
    faults = FAULTS if args.fault == "all" else (args.fault,)
    rows = []
    with httpx.Client(base_url=args.url, timeout=130) as client:
        state = client.get("/api/state").raise_for_status().json()
        if state.get("simulation") != "nav2":
            raise SystemExit("当前服务不是 Nav2 仿真")
        headers = {"X-Lab-Token": state["token"]}
        for mode in modes:
            for fault in faults:
                client.post("/api/robots/robot_1/reset", headers=headers, json={}).raise_for_status()
                client.post(
                    "/api/robots/robot_1/fault", headers=headers, json={"kind": fault}
                ).raise_for_status()
                started = time.monotonic()
                response = client.post(
                    "/api/runs",
                    headers=headers,
                    json={
                        "robots": ["robot_1"], "mode": mode, "auto_repair": True,
                        "prompt": "调查导航失败，引用现场机制证据，执行匹配的白名单修复并重放原任务。",
                    },
                ).raise_for_status()
                run_id = response.json()["run_ids"][0]
                deadline = time.monotonic() + 125
                while True:
                    run = client.get(f"/api/runs/{run_id}").raise_for_status().json()
                    if run["status"] in TERMINAL:
                        break
                    if time.monotonic() >= deadline:
                        raise SystemExit(f"评测超时：{mode}/{fault}/{run_id}")
                    time.sleep(0.5)
                row = evaluate_run(run, fault, mode, time.monotonic() - started)
                row["run_id"] = run_id
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False))
    result = {
        "generated_at": time.time(),
        "simulation": "gazebo_nav2",
        "rows": rows,
        "all_identified": all(row["identified_correctly"] for row in rows),
        "all_restored": all(row["original_task_restored"] for row in rows),
        "ai_has_no_rules_fallback": all(
            row["mode"] != "ai" or row["tool_calls"] > 0 for row in rows
        ),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if not result["all_identified"] or not result["all_restored"]:
        raise SystemExit("故障矩阵存在失败项，详情见 " + str(output))
    print("评测矩阵通过：" + str(output))


if __name__ == "__main__":
    main()
