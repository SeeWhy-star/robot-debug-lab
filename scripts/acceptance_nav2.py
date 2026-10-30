"""Live acceptance for the Gazebo/Nav2 incident, investigation and replay loop."""

import argparse
import json
import time

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--mode", choices=["ai", "rules"], default="ai")
    args = parser.parse_args()

    with httpx.Client(base_url=args.url, timeout=60) as client:
        state = client.get("/api/state").raise_for_status().json()
        if state.get("simulation") != "nav2":
            raise SystemExit("当前服务不是 Nav2 模式；请设置 RDL_SIM_BACKEND=nav2 后重启")
        headers = {"X-Lab-Token": state["token"]}

        client.post("/api/robots/robot_1/reset", headers=headers, json={}).raise_for_status()
        client.post(
            "/api/robots/robot_1/fault",
            headers=headers,
            json={"kind": "scan_interrupted"},
        ).raise_for_status()
        response = client.post(
            "/api/runs",
            headers=headers,
            json={
                "robots": ["robot_1"],
                "mode": args.mode,
                "prompt": "排查为什么机器人无法到达货架 A，引用现场证据，必要时执行受限修复并验证。",
                "auto_repair": True,
            },
        ).raise_for_status()
        run_id = response.json()["run_ids"][0]

        deadline = time.monotonic() + 120
        run = None
        while time.monotonic() < deadline:
            run = client.get(f"/api/runs/{run_id}").raise_for_status().json()
            if run["status"] not in {
                "queued",
                "reproducing",
                "navigating",
                "investigating",
                "repairing",
                "validating",
            }:
                break
            time.sleep(0.5)

    if run is None or run["status"] != "verified":
        raise SystemExit("Nav2 故障闭环未通过：" + json.dumps(run, ensure_ascii=False))
    if run.get("baseline", {}).get("success"):
        raise SystemExit("故障复现无效：修复前任务不应成功")
    if run.get("diagnosis", {}).get("cause") != "scan_interrupted":
        raise SystemExit("未正确定位雷达链路中断")
    if not run.get("validation", {}).get("success"):
        raise SystemExit("修复后原任务没有通过独立位置验收")

    print(
        json.dumps(
            {
                "run_id": run_id,
                "mode": args.mode,
                "status": run["status"],
                "diagnosis": run["diagnosis"]["cause"],
                "baseline_error": run["baseline"]["error"],
                "validation_error": run["validation"]["error"],
                "events": len(run["events"]),
                "tokens": sum(run["usage"].values()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
