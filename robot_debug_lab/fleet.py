"""Tool-grounded fleet planning with deterministic dispatch boundaries."""

from copy import deepcopy
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
from .supervisor import MissionStep


_NUMBER = r"-?(?:\d+(?:\.\d+)?|\.\d+)"
_COORDINATE_PAIR = re.compile(
    rf"[（(]\s*({_NUMBER})\s*[,，]\s*({_NUMBER})\s*[）)]"
)


def explicit_coordinates(text):
    return {
        (float(match.group(1)), float(match.group(2)))
        for match in _COORDINATE_PAIR.finditer(text)
    }


class FleetAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    robot_id: Literal["robot_1", "robot_2"]
    steps: list[MissionStep] = Field(min_length=1, max_length=8)
    rationale: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def valid_sequence(self):
        for index, step in enumerate(self.steps):
            if step.action != "simulate_charge":
                continue
            if index == 0:
                raise ValueError("simulate_charge requires charging-station arrival")
            previous = self.steps[index - 1]
            if previous.action != "navigate" or previous.destination_id != "charging_station":
                raise ValueError("simulate_charge must follow charging-station navigation")
        return self


class FleetDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    assignments: list[FleetAssignment] = Field(min_length=1, max_length=2)
    message: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def distinct_and_safe(self):
        robots = [item.robot_id for item in self.assignments]
        if len(set(robots)) != len(robots):
            raise ValueError("each robot may receive at most one ordered mission")
        charging_users = sum(
            any(step.destination_id == "charging_station" for step in item.steps)
            for item in self.assignments
        )
        if charging_users > 1:
            raise ValueError("the single charging station cannot be assigned concurrently")
        return self


def _step_schema():
    return {
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
    }


FLEET_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_fleet_state",
            "description": "Read both robots' live poses, availability and health summaries.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_destinations",
            "description": "Resolve all named places through the authoritative semantic registry.",
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array", "items": {"type": "string"},
                        "minItems": 1, "maxItems": 8,
                    }
                },
                "required": ["names"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_fleet_plan",
            "description": "Return one ordered mission per selected robot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "assignments": {
                        "type": "array", "minItems": 1, "maxItems": 2,
                        "items": {
                            "type": "object",
                            "properties": {
                                "robot_id": {"type": "string", "enum": list(config.ROBOTS)},
                                "steps": {"type": "array", "minItems": 1, "maxItems": 8,
                                          "items": _step_schema()},
                                "rationale": {"type": "string"},
                            },
                            "required": ["robot_id", "steps", "rationale"],
                            "additionalProperties": False,
                        },
                    },
                    "message": {"type": "string"},
                },
                "required": ["assignments", "message"],
                "additionalProperties": False,
            },
        },
    },
]


FLEET_SYSTEM = """You are a fleet task planner for exactly two robots in one Nav2 + Gazebo
warehouse. First call get_fleet_state. Call resolve_destinations for every named place, then call
finish_fleet_plan. Assign work using live availability, health and distance; explain the assignment
in concise Chinese. Preserve explicit robot assignments from the user. Use distinct robot_id values.
Ground named destinations only with returned object_id values and never invent coordinates. Available
actions are navigate, wait, return_to_start, enter_idle and simulate_charge. The warehouse has one
shared charging station, so never send both robots there concurrently. Charging is explicitly simulated
and simulate_charge must immediately follow navigation to charging_station. Unsupported pickup, storage
or manipulation must not be faked. Shell, velocity control, parameter changes and arbitrary tools are
forbidden. Words such as 原地, 当前位置 and current position refer to the robot's live pose: wait and
enter_idle use destination_id=null and those words are not scene destinations. Return at most eight
steps per robot and never claim that execution already happened."""


class FleetSupervisor:
    def __init__(self, store, bridge, mission_supervisor, model=None):
        self.store = store
        self.bridge = bridge
        self.mission_supervisor = mission_supervisor
        self.model = model
        self.lock = threading.Lock()

    def _fleet_state(self):
        return {
            "robots": [
                {
                    "robot_id": item["id"],
                    "pose": item.get("pose"),
                    "busy": item.get("busy", False),
                    "scan": item.get("scan"),
                    "localization": item.get("localization"),
                }
                for item in self.bridge.public()
            ],
            "shared_resources": {
                "charging_station": {"capacity": 1},
            },
        }

    def _tool(self, name, args):
        if name == "get_fleet_state" and args == {}:
            return self._fleet_state()
        if name == "resolve_destinations" and set(args) == {"names"}:
            names = args["names"]
            if not isinstance(names, list) or not 1 <= len(names) <= 8:
                raise ValueError("names must contain one to eight destinations")
            return {
                "results": [
                    self.mission_supervisor.scene.resolve_destination(value)
                    for value in names
                ]
            }
        raise ValueError("invalid or unauthorized fleet tool")

    def _recoverable_unresolved_query(self, query):
        """Separate planner phrasing errors from genuinely unknown objects."""
        normalized = self.mission_supervisor.scene._normalize(query)
        if normalized in {
            "原地", "当前位置", "当前地点", "这里", "此处",
            "currentposition", "currentlocation", "here",
        }:
            return True
        for item in self.mission_supervisor.scene.list_scene_objects():
            aliases = [item["object_id"], item["label"], *item.get("aliases", [])]
            if any(
                self.mission_supervisor.scene._normalize(alias) in normalized
                for alias in aliases
            ):
                return True
        return False

    def _materialize(self, draft, auto_repair):
        assignments = []
        for assignment in draft.assignments:
            steps = self.mission_supervisor._materialize(
                assignment.steps, auto_repair, assignment.robot_id
            )
            assignments.append({
                "robot_id": assignment.robot_id,
                "steps": steps,
                "rationale": assignment.rationale,
            })
        return assignments

    def plan(
        self,
        prompt,
        auto_repair=True,
        allow_handoff=True,
        traffic_wait_timeout_seconds=45.0,
    ):
        if not self.lock.acquire(False):
            raise ValueError("车队Agent正在规划另一条任务，请稍后重试")
        plan = {
            "id": uuid.uuid4().hex[:12],
            "prompt": prompt,
            "request_text": prompt,
            "created": time.time(),
            "backend": config.AGENT_BACKEND,
            "plan_type": "fleet",
            "status": "planning",
            "assignments": [],
            "run_ids": [],
            "allow_handoff": bool(allow_handoff),
            "traffic_wait_timeout_seconds": float(traffic_wait_timeout_seconds),
            "handoffs": [],
        }
        remote = None
        user_coordinates = explicit_coordinates(prompt)
        try:
            self.store.save_plan(plan)
            model = self.model
            if model is None:
                if config.AGENT_BACKEND == "openfang":
                    remote = OpenFangModel(None, plan["id"] + "-fleet")
                    model = remote
                else:
                    model = CompatibleModel()
            messages = [
                {"role": "system", "content": FLEET_SYSTEM},
                {"role": "user", "content": prompt},
            ]
            usage = {"prompt_tokens": 0, "completion_tokens": 0}
            fleet_observed = False
            observed_objects = set()
            unresolved_queries = set()
            trace = []
            seen = set()
            for _ in range(10):
                message, used = model.complete(messages, FLEET_TOOLS)
                for key in usage:
                    usage[key] += int(used.get(key, 0))
                calls = message.get("tool_calls") or []
                if not calls or len(calls) > 4:
                    messages.append({"role": "user", "content": "请只调用给定车队工具。"})
                    continue
                messages.append(message)
                retry = False
                for call in calls:
                    name = call["function"]["name"]
                    try:
                        args = json.loads(call["function"].get("arguments", "{}"))
                    except (TypeError, ValueError):
                        raise ModelError("车队Agent返回了无效工具参数") from None
                    if name == "finish_fleet_plan":
                        if not fleet_observed:
                            raise ModelError("车队计划缺少实时车队状态证据")
                        try:
                            draft = FleetDraft.model_validate(args)
                        except ValidationError as error:
                            messages.append({
                                "role": "tool", "tool_call_id": call["id"],
                                "content": json.dumps({
                                    "accepted": False,
                                    "errors": error.errors(
                                        include_input=False,
                                        include_url=False,
                                        include_context=False,
                                    ),
                                }, ensure_ascii=False),
                            })
                            messages.append({
                                "role": "user",
                                "content": "计划未通过车队安全Schema，请修正后再次调用finish_fleet_plan。",
                            })
                            retry = True
                            break
                        if unresolved_queries:
                            raise ValueError("车队任务包含未登记地点，请先明确目标")
                        for assignment in draft.assignments:
                            if self.bridge.locks[assignment.robot_id].locked():
                                raise ValueError(f"{assignment.robot_id} 正在执行任务")
                            for step in assignment.steps:
                                if step.destination_id and step.destination_id not in observed_objects:
                                    raise ModelError("车队计划引用了未经语义工具查询的地点")
                                if (
                                    step.action == "navigate"
                                    and step.destination_id is None
                                    and (step.x, step.y) not in user_coordinates
                                ):
                                    raise ModelError("车队计划引用了用户未明确提供的坐标")
                        plan.update(
                            status="ready",
                            message=draft.message,
                            assignments=self._materialize(draft, auto_repair),
                            usage=usage,
                            tool_trace=trace,
                        )
                        return plan
                    signature = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
                    if signature in seen:
                        raise ModelError("车队Agent重复调用了相同工具")
                    seen.add(signature)
                    result = self._tool(name, args)
                    if name == "get_fleet_state":
                        fleet_observed = True
                    elif name == "resolve_destinations":
                        observed_objects.update(
                            item["object_id"] for item in result["results"]
                            if item.get("resolved")
                        )
                        unresolved = [
                            item["query"] for item in result["results"]
                            if not item.get("resolved")
                        ]
                        unresolved_queries.update(
                            query for query in unresolved
                            if not self._recoverable_unresolved_query(query)
                        )
                        if unresolved:
                            result["instruction"] = (
                                "未解析项不能作为导航目标。原地/当前位置等待请使用"
                                "destination_id=null；已登记对象请改用其准确名称重新查询。"
                            )
                    trace.append({"name": name, "arguments": args, "result": result})
                    messages.append({
                        "role": "tool", "tool_call_id": call["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    })
                if retry:
                    continue
            raise ModelError("车队Agent未在限定轮次内返回有效计划")
        except Exception:
            plan.update(status="failed", message="车队任务规划失败，未执行机器人动作")
            raise
        finally:
            if remote:
                try:
                    remote.close()
                except ModelError:
                    plan["cleanup_warning"] = remote.agent_id
            self.store.save_plan(plan)
            self.lock.release()
