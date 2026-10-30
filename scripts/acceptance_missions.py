"""Live acceptance for deterministic routing, scene grounding, and ordered missions."""

import argparse
import json
import time

import httpx


TERMINAL = {"verified", "unresolved", "failed", "canceled", "diagnosed", "interrupted"}


def post_json(client, path, *, headers, json_body):
    response = client.post(path, headers=headers, json=json_body)
    if response.is_error:
        try:
            detail = response.json().get("detail", response.reason_phrase)
        except ValueError:
            detail = response.reason_phrase
        raise RuntimeError(f"{path} 返回 {response.status_code}: {detail}")
    return response.json()


def wait_run(client, run_id, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}").raise_for_status().json()
        if run["status"] in TERMINAL:
            return run
        time.sleep(0.5)
    raise RuntimeError(f"任务超时：{run_id}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=130) as client:
        state = client.get("/api/state").raise_for_status().json()
        headers = {"X-Lab-Token": state["token"]}
        if args.reset:
            client.post("/api/robots/robot_1/reset", headers=headers, json={}).raise_for_status()

        simple = post_json(
            client, "/api/assistant/tasks", headers=headers,
            json_body={"prompt": "去实体障碍箱旁边", "auto_repair": True},
        )
        simple_plan = simple["plan"]
        if simple_plan["backend"] != "deterministic" or sum(simple_plan["usage"].values()) != 0:
            raise SystemExit("简单任务错误调用了模型")
        step = simple_plan["steps"][0]
        if step["destination_id"] != "temporary_crate" or [step["x"], step["y"]] != [0.55, 0.5]:
            raise SystemExit("障碍箱没有使用后端安全接近点")
        simple_run = wait_run(client, simple["run_ids"][0])
        if simple_run["status"] != "verified":
            raise SystemExit("简单语义导航未通过")

        unknown = post_json(
            client, "/api/assistant/tasks", headers=headers,
            json_body={"prompt": "去月球仓库", "auto_repair": True},
        )
        if unknown["run_ids"] or unknown["plan"]["status"] != "needs_clarification":
            raise SystemExit("未知对象没有进入澄清状态")
        if client.get("/api/state").raise_for_status().json()["active_targets"]:
            raise SystemExit("拒绝指令后仍显示陈旧活动目标")

        unsupported = post_json(
            client, "/api/assistant/tasks", headers=headers,
            json_body={"prompt": "去货架 A 拿货，然后去充电", "auto_repair": True},
        )
        unsupported_plan = unsupported["plan"]
        capability_tools = [
            call["name"] for call in unsupported_plan.get("scene_tool_calls", [])
        ]
        if (
            unsupported["run_ids"]
            or unsupported_plan["status"] != "needs_clarification"
            or unsupported_plan["steps"]
            or "list_robot_capabilities" not in capability_tools
        ):
            raise SystemExit("未支持的取货意图没有通过能力查询安全拒绝")

        complex_result = post_json(
            client, "/api/assistant/tasks", headers=headers,
            json_body={
                "prompt": "机器人去实体障碍箱回来后去充电",
                "auto_repair": True,
            },
        )
        complex_plan = complex_result["plan"]
        if complex_plan["backend"] == "deterministic" or not complex_plan.get("scene_tool_calls"):
            raise SystemExit("复杂任务没有通过模型和场景工具规划")
        complex_run = wait_run(client, complex_result["run_ids"][0])
        if complex_run["status"] != "verified":
            raise SystemExit("复杂多步骤任务未通过：" + json.dumps(complex_run, ensure_ascii=False))
        completed = [step["id"] for step in complex_run["steps"] if step["status"] == "completed"]
        if len(completed) != len(complex_run["steps"]):
            raise SystemExit("复杂任务没有按顺序完成全部步骤")

    print(json.dumps({
        "simple_plan": simple_plan["id"],
        "simple_model_tokens": sum(simple_plan["usage"].values()),
        "safe_crate_goal": [0.55, 0.5],
        "unknown_status": unknown["plan"]["status"],
        "unsupported_status": unsupported_plan["status"],
        "unsupported_tools": capability_tools,
        "complex_plan": complex_plan["id"],
        "complex_scene_tools": [call["name"] for call in complex_plan["scene_tool_calls"]],
        "complex_steps": [step["action"] for step in complex_run["steps"]],
        "complex_status": complex_run["status"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
