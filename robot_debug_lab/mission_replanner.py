"""Evidence-grounded recovery of a failed semantic navigation step."""

import json
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import config
from .agents import Canceled, CompatibleModel, ModelError
from .openfang import OpenFangModel
from .semantic_scene import SCENE


class ReplanDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["retry_with_approach", "abort"]
    failed_step_id: str = Field(min_length=1, max_length=20)
    approach_id: str | None = Field(default=None, max_length=80)
    reason: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def valid_decision(self):
        if (self.decision == "retry_with_approach") != (self.approach_id is not None):
            raise ValueError("retry_with_approach requires approach_id; abort forbids it")
        return self


FINISH_REPLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string", "enum": ["retry_with_approach", "abort"]
        },
        "failed_step_id": {"type": "string"},
        "approach_id": {"type": ["string", "null"]},
        "reason": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "decision", "failed_step_id", "approach_id", "reason", "evidence_ids"
    ],
    "additionalProperties": False,
}


REPLAN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_mission_state",
            "description": "Read completed, failed and remaining mission steps.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_approach_options",
            "description": (
                "List reviewed approach poses for the failed semantic destination and ask "
                "the real Nav2 planner for reachability. Coordinates are server-owned."
            ),
            "parameters": {
                "type": "object",
                "properties": {"object_id": {"type": "string"}},
                "required": ["object_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_replan",
            "description": "Select one observed reachable approach or abort safely.",
            "parameters": FINISH_REPLAN_SCHEMA,
        },
    },
]


REPLAN_SYSTEM = """You recover one failed semantic navigation step in a Nav2 mission.
Read mission state and reviewed approach options. Preserve every completed step and every later
user-requested step. You may only select a reachable approach_id returned by the tool for the same
failed destination; never invent coordinates, destinations, actions, or claim success. The failed
approach has real execution evidence and cannot be selected again. If no alternative is reachable,
abort. Cite the E identifiers returned by both observation tools. Explain the decision in Chinese.
Finish with finish_replan. Do not request shell, velocity, parameter, or mutation tools.
"""


class MissionReplanner:
    def __init__(self, store, bridge, scene=None, model=None):
        self.store = store
        self.bridge = bridge
        self.scene = scene or SCENE
        self.model = model

    @staticmethod
    def _mission_state(run, failed_index):
        steps = run["steps"]
        return {
            "request": run["prompt"],
            "completed_steps": [
                {"id": step["id"], "action": step["action"], "description": step["description"]}
                for step in steps[:failed_index]
                if step.get("status") == "completed"
            ],
            "failed_step": {
                key: steps[failed_index].get(key)
                for key in [
                    "id", "action", "destination_id", "approach_id", "description",
                    "baseline", "failure_reason",
                ]
            },
            "remaining_steps": [
                {"id": step["id"], "action": step["action"], "description": step["description"]}
                for step in steps[failed_index + 1:]
            ],
        }

    def _approach_options(self, run, failed_index, object_id):
        failed = run["steps"][failed_index]
        if object_id != failed.get("destination_id"):
            raise ValueError("只能查询失败步骤的语义目标")
        options = []
        for approach in self.scene.get_approach_options(object_id):
            pose = approach["pose"]
            if approach["approach_id"] == failed.get("approach_id"):
                route = {
                    "reachable": False,
                    "target": [pose["x"], pose["y"]],
                    "reason": "actual_navigation_attempt_failed",
                }
            else:
                route = self.bridge.plan_path(
                    run["robot_id"], float(pose["x"]), float(pose["y"])
                )
            options.append({
                "approach_id": approach["approach_id"],
                "label": approach["label"],
                "reachable": bool(route["reachable"]),
                "route_evidence": route,
            })
        return {"object_id": object_id, "options": options}

    def replan(self, run, failed_index, cancel):
        failed = run["steps"][failed_index]
        if failed.get("action") != "navigate" or not failed.get("destination_id"):
            return None
        remote = None
        model = self.model
        if model is None:
            if config.AGENT_BACKEND == "openfang":
                remote = OpenFangModel(run["robot_id"], run["id"] + "-replan")
                model = remote
            else:
                model = CompatibleModel()
        messages = [
            {"role": "system", "content": REPLAN_SYSTEM},
            {"role": "user", "content": json.dumps({
                "navigation_failure": failed.get("baseline"),
                "failed_step_id": failed["id"],
            }, ensure_ascii=False)},
        ]
        evidence = {}
        observed_options = {}
        protocol_retries = 0
        try:
            for round_number in range(6):
                if cancel.is_set():
                    raise Canceled()
                self.store.event(run, "model", "模型评估任务级重规划", {
                    "round": round_number + 1,
                    "backend": config.AGENT_BACKEND,
                })
                try:
                    message, usage = model.complete(messages, REPLAN_TOOLS)
                except ModelError as error:
                    if "valid gateway tool requests" not in str(error) or protocol_retries >= 1:
                        raise
                    protocol_retries += 1
                    messages.append({
                        "role": "user",
                        "content": "只调用给定工具，并使用 finish_replan 结束。",
                    })
                    continue
                for key in ["prompt_tokens", "completion_tokens"]:
                    run["usage"][key] += int(usage.get(key, 0))
                self.store.save(run)
                calls = message.get("tool_calls") or []
                if not calls or len(calls) > 3:
                    messages.append({
                        "role": "user", "content": "调用观察工具并用 finish_replan 结束。"
                    })
                    continue
                messages.append(message)
                for call in calls:
                    name = call["function"]["name"]
                    try:
                        args = json.loads(call["function"].get("arguments", "{}"))
                    except (TypeError, ValueError):
                        raise ModelError("重规划工具参数不是有效 JSON") from None
                    if name == "finish_replan":
                        try:
                            decision = ReplanDecision.model_validate(args)
                        except ValidationError as error:
                            result = {
                                "accepted": False,
                                "errors": [
                                    {"loc": list(item["loc"]), "msg": item["msg"]}
                                    for item in error.errors(include_input=False, include_url=False)
                                ],
                            }
                            messages.append({
                                "role": "tool", "tool_call_id": call["id"],
                                "content": json.dumps(result, ensure_ascii=False),
                            })
                            continue
                        if decision.failed_step_id != failed["id"]:
                            raise ModelError("重规划试图修改其他步骤")
                        if any(item not in evidence for item in decision.evidence_ids):
                            raise ModelError("重规划引用了不存在的证据")
                        cited_tools = {
                            evidence[item]["title"] for item in decision.evidence_ids
                        }
                        if not {
                            "get_mission_state", "list_approach_options"
                        } <= cited_tools:
                            raise ModelError("重规划必须引用任务状态和路径选项证据")
                        if decision.decision == "abort":
                            return {
                                **decision.model_dump(), "replacement_step": None
                            }
                        option = observed_options.get(decision.approach_id)
                        if not option or not option["reachable"]:
                            raise ModelError("重规划选择了未经验证或不可达的接近点")
                        approach = self.scene.get_approach_pose(
                            failed["destination_id"], decision.approach_id
                        )
                        replacement = deepcopy(failed)
                        replacement.update(
                            x=float(approach["pose"]["x"]),
                            y=float(approach["pose"]["y"]),
                            approach_id=decision.approach_id,
                            pose_source="semantic_scene_registry_replan",
                            status="pending",
                            failure_reason=None,
                        )
                        return {
                            **decision.model_dump(), "replacement_step": replacement
                        }
                    try:
                        if name == "get_mission_state" and args == {}:
                            data = self._mission_state(run, failed_index)
                        elif name == "list_approach_options" and set(args) == {"object_id"}:
                            data = self._approach_options(run, failed_index, args["object_id"])
                            observed_options.update({
                                option["approach_id"]: option for option in data["options"]
                            })
                        else:
                            raise ValueError("无效或越权的重规划工具调用")
                        event = self.store.event(run, "tool", name, data)
                        evidence[event["id"]] = event
                        result = event
                    except (ValueError, KeyError, TypeError) as error:
                        result = {"error": str(error)}
                    messages.append({
                        "role": "tool", "tool_call_id": call["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    })
            raise ModelError("Agent 在限定轮次内没有返回有效重规划")
        finally:
            if remote:
                try:
                    remote.close()
                except ModelError:
                    self.store.event(run, "warning", "重规划 Agent 会话清理失败")
