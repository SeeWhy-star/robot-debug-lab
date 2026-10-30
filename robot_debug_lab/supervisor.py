"""Deterministic simple routing and tool-grounded complex mission planning."""

import json
import re
import threading
import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import config
from .agents import CompatibleModel, ModelError
from .openfang import OpenFangModel
from .semantic_scene import SCENE, SemanticScene


class NavigationTask(BaseModel):
    """Legacy single-navigation contract used by the diagnosis entry point."""

    model_config = ConfigDict(extra="forbid", strict=True)
    robot_id: Literal["robot_1"]
    x: float = Field(ge=-4.0, le=4.0, allow_inf_nan=False)
    y: float = Field(ge=-4.0, le=4.0, allow_inf_nan=False)
    auto_repair: bool


class MissionStep(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["navigate", "wait", "return_to_start", "enter_idle", "simulate_charge"]
    destination_id: str | None = Field(default=None, max_length=80)
    x: float | None = Field(default=None, ge=-4.0, le=4.0, allow_inf_nan=False)
    y: float | None = Field(default=None, ge=-4.0, le=4.0, allow_inf_nan=False)
    duration_seconds: float | None = Field(default=None, ge=0.0, le=30.0, allow_inf_nan=False)
    description: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_action_fields(self):
        if self.action == "navigate":
            has_object = self.destination_id is not None
            has_coordinate = self.x is not None and self.y is not None
            if has_object == has_coordinate:
                raise ValueError("navigate requires one destination object or one coordinate")
        elif any(value is not None for value in (self.destination_id, self.x, self.y)):
            raise ValueError("only navigate accepts a destination")
        if self.action in {"wait", "simulate_charge"}:
            if self.duration_seconds is None:
                raise ValueError(f"{self.action} requires duration_seconds")
        elif self.duration_seconds is not None:
            raise ValueError("duration is only valid for wait or simulate_charge")
        return self


class PlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    steps: list[MissionStep] = Field(max_length=8)
    message: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def valid_mission_sequence(self):
        for index, step in enumerate(self.steps):
            if step.action != "simulate_charge":
                continue
            if index == 0:
                raise ValueError("simulate_charge requires arrival at charging_station first")
            previous = self.steps[index - 1]
            if previous.action != "navigate" or previous.destination_id != "charging_station":
                raise ValueError(
                    "simulate_charge must immediately follow navigation to charging_station"
                )
        return self


FINISH_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["navigate", "wait", "return_to_start", "enter_idle", "simulate_charge"],
                    },
                    "destination_id": {"type": ["string", "null"]},
                    "x": {"type": ["number", "null"], "minimum": -4.0, "maximum": 4.0},
                    "y": {"type": ["number", "null"], "minimum": -4.0, "maximum": 4.0},
                    "duration_seconds": {"type": ["number", "null"], "minimum": 0.0, "maximum": 30.0},
                    "description": {"type": "string"},
                },
                "required": ["action", "description"],
                "additionalProperties": False,
            },
        },
        "message": {"type": "string"},
    },
    "required": ["steps", "message"],
    "additionalProperties": False,
}


SCENE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_scene_objects",
            "description": "List authoritative semantic objects. Coordinates are not inferred from labels.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    *[
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {"object_id": {"type": "string"}},
                    "required": ["object_id"],
                    "additionalProperties": False,
                },
            },
        }
        for name, description in [
            ("get_object_pose", "Get an object's Gazebo-aligned entity pose. This is not a navigation goal."),
            ("get_safe_approach_pose", "Get the reviewed collision-free approach pose for an object."),
        ]
    ],
    {
        "type": "function",
        "function": {
            "name": "get_robot_state",
            "description": "Read the current robot state from the live backend.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_robot_capabilities",
            "description": (
                "List the executable robot skills and their completion evidence. "
                "Use this before responding to an intent that may require an unsupported skill."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_destination",
            "description": "Resolve a natural-language destination through the authoritative registry.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_destinations",
            "description": (
                "Resolve every destination name in a multi-stop request in one authoritative "
                "registry lookup. Prefer this over several resolve_destination calls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 8,
                    }
                },
                "required": ["names"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_plan",
            "description": "Return ordered steps, or steps=[] with a clarification question.",
            "parameters": FINISH_PLAN_SCHEMA,
        },
    },
]


ROBOT_CAPABILITIES = [
    {
        "action": "navigate",
        "description": "Use Nav2 to reach a coordinate or a registered object's safe approach pose.",
        "completion_evidence": "Nav2 result plus final Gazebo odometry tolerance",
    },
    {
        "action": "wait",
        "description": "Remain stopped for a bounded duration.",
        "completion_evidence": "deterministic timer event",
    },
    {
        "action": "return_to_start",
        "description": "Navigate to the registered start pose.",
        "completion_evidence": "Nav2 result plus final Gazebo odometry tolerance",
    },
    {
        "action": "enter_idle",
        "description": "Enter the mission executor's idle state.",
        "completion_evidence": "executor state transition",
    },
    {
        "action": "simulate_charge",
        "description": "Run an explicitly labelled charging simulation after reaching the station.",
        "completion_evidence": "charging_started and charging_completed simulation events",
    },
]


MISSION_SYSTEM = """Plan a mission for one robot in a Nav2 + Gazebo warehouse.
Ground every named destination with scene tools. For two or more names, prefer one
resolve_destinations call. Never invent coordinates for named objects; finish_plan uses returned
object_id values and the server binds safe approach poses. Available steps are navigate, wait,
return_to_start, enter_idle, and simulate_charge. Charging is explicitly simulated. For an unknown
object, return steps=[] and ask a concise Chinese question. For pickup, storage, inspection, or any
unsupported operation, query list_robot_capabilities, return steps=[], explain the gap, and offer
an executable alternative. Never pretend an unsupported operation is waiting or completed. Preserve
the requested order, use at most 8 steps, and never claim execution. Shell, velocity control, fault
injection, parameter changes, and arbitrary tools are forbidden. Use Chinese descriptions/messages.

Tool budget: for a supported navigation/wait/idle/charge request, call resolve_destinations exactly
once with all named places, then finish_plan. Do not call list_scene_objects unless a resolution is
unknown. Do not call list_robot_capabilities unless the user requests an unsupported operation. Do
not call get_robot_state unless the plan depends on current robot state. Never repeat a tool call.
simulate_charge must immediately follow a navigate step to charging_station.
Charging example: [{"action":"navigate","destination_id":"charging_station","description":"前往充电站"},
{"action":"simulate_charge","duration_seconds":5,"description":"执行仿真充电"}].
"""


_COORDINATE = re.compile(
    r"^(?:(?:请|麻烦)\s*)?(?:让\s*)?(?:机器人\s*)?"
    r"(?:去|到|前往|导航到)\s*[（(]\s*(-?(?:\d+(?:\.\d+)?|\.\d+))\s*[,，]\s*"
    r"(-?(?:\d+(?:\.\d+)?|\.\d+))\s*[）)]$"
)
_DESTINATION_COMMAND = re.compile(
    r"^(?:(?:请|麻烦)\s*)?(?:让\s*)?(?:机器人\s*)?"
    r"(?:去|前往|导航到|到|回到|回)\s*(.+?)$"
)


class Supervisor:
    def __init__(self, store, model=None, scene: SemanticScene | None = None, robot_state=None):
        self.store = store
        self.model = model
        self.scene = scene or SCENE
        self.robot_state = robot_state or (lambda: {"robot_id": "robot_1", "available": False})
        self.lock = threading.Lock()

    def _simple_draft(self, prompt):
        """Parse one complete atomic command or defer the whole request to the model.

        The deterministic path is intentionally accept-only: it succeeds only
        when the entire utterance is one known destination or one coordinate.
        It never decides that an unresolved remainder is a destination name.
        """
        command = re.sub(r"[。.!！?？]+$", "", prompt.strip()).strip()
        coordinate = _COORDINATE.fullmatch(command)
        if coordinate:
            x, y = map(float, coordinate.groups())
            if not (-4.0 <= x <= 4.0 and -4.0 <= y <= 4.0):
                return PlanDraft(steps=[], message="坐标超出仓库导航范围，请提供 -4.0 到 4.0 内的坐标。")
            return PlanDraft(
                steps=[MissionStep(action="navigate", x=x, y=y, description=f"导航到坐标 ({x:g}, {y:g})")],
                message=f"已用确定性坐标校验生成导航任务：({x:g}, {y:g})。",
            )
        destination = _DESTINATION_COMMAND.fullmatch(command)
        if destination is None:
            return None
        query = destination.group(1).strip()
        resolved = self.scene.resolve_destination(query)
        if resolved["resolved"]:
            object_id = resolved["object_id"]
            action = "return_to_start" if object_id == "start" else "navigate"
            step = MissionStep(
                action=action,
                destination_id=None if action == "return_to_start" else object_id,
                description=f"前往{resolved['label']}的安全接近点" if action == "navigate" else "返回起点",
            )
            return PlanDraft(steps=[step], message=f"已由确定性语义地点表解析：{resolved['label']}。")
        # Unknown or compound text is model territory.  The model can inspect
        # the scene, recognize multiple actions, and ask a grounded question.
        return None

    def _tool(self, name, args):
        if name == "list_scene_objects" and args == {}:
            return self.scene.list_scene_objects()
        if name == "get_object_pose" and set(args) == {"object_id"}:
            return self.scene.get_object_pose(args["object_id"])
        if name == "get_safe_approach_pose" and set(args) == {"object_id"}:
            return self.scene.get_safe_approach_pose(args["object_id"])
        if name == "get_robot_state" and args == {}:
            return self.robot_state()
        if name == "list_robot_capabilities" and args == {}:
            return ROBOT_CAPABILITIES
        if name == "resolve_destination" and set(args) == {"name"}:
            return self.scene.resolve_destination(args["name"])
        if name == "resolve_destinations" and set(args) == {"names"}:
            names = args["names"]
            if (
                not isinstance(names, list)
                or not 1 <= len(names) <= 8
                or any(not isinstance(item, str) or not item.strip() for item in names)
            ):
                raise ValueError("names 必须包含 1 到 8 个非空地点名称")
            return {"results": [self.scene.resolve_destination(item) for item in names]}
        raise ValueError("无效或越权的场景工具调用")

    def _model_draft(self, turns, plan):
        remote = None
        model = self.model
        if model is None:
            if config.AGENT_BACKEND == "openfang":
                remote = OpenFangModel(None, plan["id"])
                model = remote
            else:
                model = CompatibleModel()
        messages = [
            {"role": "system", "content": MISSION_SYSTEM},
            {"role": "user", "content": json.dumps({"conversation": turns}, ensure_ascii=False)},
        ]
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        observed_objects = set()
        tool_trace = []
        seen_tool_calls = set()
        protocol_retries = 0
        plan_validation_retries = 0
        try:
            for _ in range(10):
                try:
                    message, used = model.complete(messages, SCENE_TOOLS)
                except ModelError as error:
                    if (
                        "valid gateway tool requests" not in str(error)
                        or protocol_retries >= 2
                    ):
                        raise
                    protocol_retries += 1
                    plan["protocol_retries"] = protocol_retries
                    messages.append({
                        "role": "user",
                        "content": (
                            "Your previous response did not match the gateway tool protocol. "
                            "Reply only with one JSON object shaped exactly like "
                            '{"calls":[{"name":"TOOL_NAME","arguments":{}}]}. '
                            "Use only supplied tool names, use scene tools before finish_plan, "
                            "and do not answer in free text."
                        ),
                    })
                    continue
                for key in usage:
                    usage[key] += int(used.get(key, 0))
                calls = message.get("tool_calls") or []
                if not calls or len(calls) > 6:
                    messages.append({"role": "user", "content": "请只调用给定工具，并用 finish_plan 结束。"})
                    continue
                messages.append(message)
                for call in calls:
                    name = call["function"]["name"]
                    try:
                        args = json.loads(call["function"].get("arguments", "{}"))
                    except (TypeError, ValueError):
                        raise ModelError("Assistant 返回的计划工具参数无效") from None
                    if name == "finish_plan":
                        try:
                            draft = PlanDraft.model_validate(args)
                        except ValidationError as error:
                            validation_errors = [
                                {
                                    "type": item["type"],
                                    "loc": list(item["loc"]),
                                    "msg": item["msg"],
                                }
                                for item in error.errors(
                                    include_input=False, include_url=False
                                )
                            ]
                            plan["last_plan_validation_errors"] = validation_errors
                            if plan_validation_retries >= 2:
                                raise ModelError("Assistant 返回的计划无效，未执行任何机器人动作") from None
                            plan_validation_retries += 1
                            plan["plan_validation_retries"] = plan_validation_retries
                            messages.append({
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": json.dumps({
                                    "accepted": False,
                                    "reason": "finish_plan_schema_validation_failed",
                                    "errors": validation_errors,
                                }, ensure_ascii=False),
                            })
                            messages.append({
                                "role": "user",
                                "content": (
                                    "The proposed finish_plan failed schema validation and no "
                                    "robot action was executed. Use exactly the supplied action "
                                    "enum and fields. For any unsupported operation, return "
                                    "steps=[] with a Chinese capability-gap explanation and an "
                                    "available alternative. simulate_charge accepts only action, "
                                    "duration_seconds and description, and must immediately follow "
                                    "navigate to charging_station. Call finish_plan again."
                                ),
                            })
                            break
                        for step in draft.steps:
                            if step.destination_id:
                                if step.destination_id not in observed_objects:
                                    raise ModelError("复杂计划引用了未经场景工具查询的对象")
                                self.scene.get_safe_approach_pose(step.destination_id)
                        plan["scene_tool_calls"] = tool_trace
                        return draft, usage, remote
                    signature = (name, json.dumps(args, ensure_ascii=False, sort_keys=True))
                    if signature in seen_tool_calls:
                        result = {
                            "error": "duplicate_tool_call",
                            "instruction": "已有相同观测，请直接调用 finish_plan。",
                        }
                        tool_trace.append({
                            "name": name, "arguments": args, "result": result,
                            "rejected": True,
                        })
                        messages.append({
                            "role": "tool", "tool_call_id": call["id"],
                            "content": json.dumps(result, ensure_ascii=False),
                        })
                        continue
                    seen_tool_calls.add(signature)
                    try:
                        result = self._tool(name, args)
                        if name == "list_scene_objects":
                            observed_objects.update(item["object_id"] for item in result)
                        elif name == "resolve_destinations":
                            observed_objects.update(
                                item["object_id"]
                                for item in result["results"]
                                if item.get("resolved")
                            )
                        elif isinstance(result, dict) and result.get("object_id"):
                            observed_objects.add(result["object_id"])
                    except (ValueError, TypeError, KeyError) as error:
                        result = {"error": str(error)}
                    tool_trace.append({"name": name, "arguments": args, "result": result})
                    messages.append({
                        "role": "tool", "tool_call_id": call["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    })
            raise ModelError("Assistant 在 10 轮内没有返回有效的结构化任务")
        except Exception:
            if remote:
                try:
                    remote.close()
                except ModelError:
                    pass
            raise

    def _materialize(self, steps, auto_repair):
        materialized = []
        for index, step in enumerate(steps, 1):
            item = step.model_dump()
            item.update(id=f"S{index}", status="pending", auto_repair=auto_repair)
            if step.action == "return_to_start":
                item["destination_id"] = "start"
            if item.get("destination_id"):
                approach = self.scene.get_safe_approach_pose(item["destination_id"])
                x, y = self.scene.navigation_pose(
                    item["destination_id"], approach["approach_id"]
                )
                item.update(
                    x=x,
                    y=y,
                    approach_id=approach["approach_id"],
                    pose_source="semantic_scene_registry",
                )
            materialized.append(item)
        return materialized

    def plan(self, prompt, auto_repair, parent_id=None):
        if not self.lock.acquire(False):
            raise ValueError("Assistant 正在解析另一条指令，请稍后再试")
        plan = {
            "id": uuid.uuid4().hex[:12], "prompt": prompt, "created": time.time(),
            "backend": "deterministic", "status": "planning", "steps": [], "run_ids": [],
        }
        remote = None
        parent = None
        try:
            turns = []
            if parent_id:
                parent = self.store.get_plan(parent_id)
                if not parent or parent["status"] != "needs_clarification":
                    raise ValueError("只能补充等待澄清的计划；已执行的任务请新建指令")
                turns = list(parent.get("turns", []))
                if len(turns) >= 10:
                    raise ValueError("已达到五轮补充上限，请新建完整指令")
                auto_repair = auto_repair and parent.get("auto_repair_ceiling", False)
                plan["parent_id"] = parent_id
            turns.append({"role": "user", "content": prompt})
            if sum(len(turn["content"]) for turn in turns) > 10000:
                raise ValueError("对话内容过长，请新建完整指令")
            plan.update(
                turns=turns, auto_repair_ceiling=auto_repair,
                request_text="\n".join(turn["content"] for turn in turns if turn["role"] == "user"),
            )
            self.store.save_plan(plan)
            # A clarification turn always needs the full conversation.  A
            # short follow-up such as “货架 A” must not bypass the unresolved
            # intent and become a fresh deterministic command.
            draft = None if parent else self._simple_draft(prompt)
            if draft is None:
                plan["backend"] = config.AGENT_BACKEND
                draft, usage, remote = self._model_draft(turns, plan)
                plan["usage"] = usage
            else:
                plan["usage"] = {"prompt_tokens": 0, "completion_tokens": 0}
            plan["steps"] = self._materialize(draft.steps, auto_repair)
            plan["message"] = draft.message
            plan["status"] = "ready" if plan["steps"] else "needs_clarification"
            plan["turns"].append({"role": "assistant", "content": plan["message"]})
            if parent:
                parent.update(status="continued", child_id=plan["id"])
                self.store.save_plan(parent)
            return plan
        except Exception:
            plan.update(status="failed", message="任务解析失败，未执行机器人动作")
            raise
        finally:
            try:
                if remote:
                    try:
                        remote.close()
                    except ModelError:
                        plan["cleanup_warning"] = remote.agent_id
                self.store.save_plan(plan)
            finally:
                self.lock.release()
