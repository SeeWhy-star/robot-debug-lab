import json

import pytest

from robot_debug_lab.agents import ModelError
from robot_debug_lab.semantic_scene import SCENE
from robot_debug_lab.store import Store
from robot_debug_lab.supervisor import Supervisor


class MustNotRunModel:
    def complete(self, messages, tools):
        raise AssertionError("simple tasks must not call a model")


class ComplexPlannerModel:
    def __init__(self, finish=None):
        self.round = 0
        self.finish = finish or {
            "steps": [
                {
                    "action": "navigate", "destination_id": "shelf_a", "x": None, "y": None,
                    "duration_seconds": None, "description": "前往货架 A",
                },
                {
                    "action": "return_to_start", "destination_id": None, "x": None, "y": None,
                    "duration_seconds": None, "description": "返回起点",
                },
                {
                    "action": "wait", "destination_id": None, "x": None, "y": None,
                    "duration_seconds": 1.0, "description": "在起点等待",
                },
            ],
            "message": "按顺序执行三个步骤。",
        }

    def complete(self, messages, tools):
        self.round += 1
        if self.round == 1:
            return self.call("resolve_destination", {"name": "货架 A"}), {
                "prompt_tokens": 10, "completion_tokens": 2
            }
        return self.call("finish_plan", self.finish), {
            "prompt_tokens": 10, "completion_tokens": 4
        }

    @staticmethod
    def call(name, arguments):
        return {
            "role": "assistant",
            "tool_calls": [{
                "id": name,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }],
        }


class GroundedClarificationModel:
    """Resolve the latest request and either clarify or return one grounded step."""

    def complete(self, messages, tools):
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if not tool_messages:
            conversation = json.loads(messages[1]["content"])["conversation"]
            latest = conversation[-1]["content"]
            return ComplexPlannerModel.call(
                "resolve_destination", {"name": latest}
            ), {"prompt_tokens": 3, "completion_tokens": 1}
        resolved = json.loads(tool_messages[-1]["content"])
        if not resolved.get("resolved"):
            finish = {
                "steps": [],
                "message": "场景中没有找到该地点，请说明要前往哪个已登记地点。",
            }
        else:
            finish = {
                "steps": [{
                    "action": "navigate",
                    "destination_id": resolved["object_id"],
                    "x": None,
                    "y": None,
                    "duration_seconds": None,
                    "description": f"前往{resolved['label']}",
                }],
                "message": "已根据补充信息生成任务。",
            }
        return ComplexPlannerModel.call("finish_plan", finish), {
            "prompt_tokens": 3, "completion_tokens": 2
        }


class UnsupportedSkillModel:
    def complete(self, messages, tools):
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if not tool_messages:
            return ComplexPlannerModel.call("list_robot_capabilities", {}), {}
        return ComplexPlannerModel.call("finish_plan", {
            "steps": [],
            "message": (
                "当前机器人没有取货执行器；可以改为前往货架 A 的安全接近点并等待人工装载。"
            ),
        }), {}


@pytest.mark.parametrize(
    ("prompt", "object_id", "target"),
    [
        ("去货架 A", "shelf_a", (2.0, 0.5)),
        ("去充电站", "charging_station", (-4.0, -3.15)),
        ("回起点", "start", (-2.0, -0.5)),
        ("去实体障碍箱旁边", "temporary_crate", (0.55, 0.5)),
    ],
)
def test_simple_semantic_tasks_do_not_call_model(tmp_path, prompt, object_id, target):
    plan = Supervisor(Store(tmp_path / "runs.db"), MustNotRunModel()).plan(prompt, True)
    assert plan["backend"] == "deterministic"
    assert plan["usage"] == {"prompt_tokens": 0, "completion_tokens": 0}
    assert plan["steps"][0]["destination_id"] == object_id
    assert (plan["steps"][0]["x"], plan["steps"][0]["y"]) == target


def test_simple_coordinate_is_validated_without_model(tmp_path):
    supervisor = Supervisor(Store(tmp_path / "runs.db"), MustNotRunModel())
    plan = supervisor.plan("去 (1.25, -0.75)", False)
    assert plan["steps"][0]["x"] == 1.25
    assert plan["steps"][0]["auto_repair"] is False
    rejected = supervisor.plan("去 (9, 2)", True)
    assert rejected["status"] == "needs_clarification"
    assert rejected["steps"] == []


def test_unknown_object_clarifies_instead_of_guessing(tmp_path):
    plan = Supervisor(Store(tmp_path / "runs.db"), GroundedClarificationModel()).plan(
        "去月球仓库", True
    )
    assert plan["status"] == "needs_clarification"
    assert plan["steps"] == []
    assert plan["backend"] != "deterministic"
    assert plan["scene_tool_calls"][0]["name"] == "resolve_destination"


def test_scene_registry_distinguishes_entity_pose_from_safe_goal():
    entity = SCENE.get_object_pose("temporary_crate")
    approach = SCENE.get_safe_approach_pose("temporary_crate")
    assert entity["gazebo_entity"] == "temporary_crate"
    assert entity["pose"] == {"x": 0.55, "y": -0.5, "yaw": 0.0}
    assert approach["pose"] == {"x": 0.55, "y": 0.5, "yaw": -1.57}
    assert approach["navigation_goal"] is True


def test_complex_task_uses_scene_tools_and_structured_steps(tmp_path):
    model = ComplexPlannerModel()
    plan = Supervisor(Store(tmp_path / "runs.db"), model).plan(
        "先去货架 A，然后回起点等待。", True
    )
    assert model.round == 2
    assert [step["action"] for step in plan["steps"]] == [
        "navigate", "return_to_start", "wait"
    ]
    assert plan["steps"][0]["pose_source"] == "semantic_scene_registry"
    assert plan["scene_tool_calls"][0]["name"] == "resolve_destination"
    assert sum(plan["usage"].values()) == 26


@pytest.mark.parametrize(
    "prompt",
    [
        "机器人去实体障碍箱回来后去充电",
        "去货架 A 回来去充电站",
        "去货架 A 检查",
        "去 (1, 2) 后回起点",
    ],
)
def test_non_atomic_language_is_always_delegated_to_model(tmp_path, prompt):
    model = ComplexPlannerModel()
    plan = Supervisor(Store(tmp_path / "runs.db"), model).plan(prompt, True)
    assert model.round == 2
    assert plan["backend"] != "deterministic"
    assert plan["steps"]


def test_unsupported_operation_uses_capability_tool_and_never_fakes_completion(tmp_path):
    plan = Supervisor(Store(tmp_path / "runs.db"), UnsupportedSkillModel()).plan(
        "去货架 A 拿货，然后去充电", True
    )
    assert plan["status"] == "needs_clarification"
    assert plan["steps"] == []
    assert plan["scene_tool_calls"][0]["name"] == "list_robot_capabilities"
    assert "没有取货执行器" in plan["message"]


def test_invalid_unsupported_step_is_returned_to_model_for_schema_repair(tmp_path):
    class SelfRepairingModel:
        def __init__(self):
            self.round = 0

        def complete(self, messages, tools):
            self.round += 1
            if self.round == 1:
                return ComplexPlannerModel.call("finish_plan", {
                    "steps": [{
                        "action": "pickup",
                        "destination_id": "shelf_a",
                        "description": "在货架 A 拿货",
                    }],
                    "message": "执行取货。",
                }), {}
            assert any(
                "finish_plan_schema_validation_failed" in message.get("content", "")
                for message in messages
            )
            return ComplexPlannerModel.call("finish_plan", {
                "steps": [],
                "message": "当前没有取货技能，可以改为到货架 A 等待人工装载。",
            }), {}

    model = SelfRepairingModel()
    plan = Supervisor(Store(tmp_path / "runs.db"), model).plan(
        "去货架 A 拿货", True
    )
    assert model.round == 2
    assert plan["plan_validation_retries"] == 1
    assert plan["status"] == "needs_clarification"
    assert plan["steps"] == []


def test_model_validator_errors_are_serializable_and_can_be_repaired(tmp_path):
    class ConflictingPoseModel:
        def __init__(self):
            self.round = 0

        def complete(self, messages, tools):
            self.round += 1
            if self.round == 1:
                return ComplexPlannerModel.call("list_scene_objects", {}), {}
            if self.round == 2:
                return ComplexPlannerModel.call("finish_plan", {
                    "steps": [{
                        "action": "navigate",
                        "destination_id": "shelf_a",
                        "x": 2.0,
                        "y": 0.5,
                        "description": "前往货架 A",
                    }],
                    "message": "执行。",
                }), {}
            return ComplexPlannerModel.call("finish_plan", {
                "steps": [{
                    "action": "navigate",
                    "destination_id": "shelf_a",
                    "x": None,
                    "y": None,
                    "duration_seconds": None,
                    "description": "前往货架 A",
                }],
                "message": "已修正。",
            }), {}

    store = Store(tmp_path / "runs.db")
    plan = Supervisor(store, ConflictingPoseModel()).plan("去货架 A 看看", True)
    assert plan["steps"][0]["destination_id"] == "shelf_a"
    assert plan["last_plan_validation_errors"][0]["type"] == "value_error"
    assert store.get_plan(plan["id"])["last_plan_validation_errors"] == plan[
        "last_plan_validation_errors"
    ]


def test_complex_react_plan_has_room_for_multiple_observations_and_repair(tmp_path):
    class LongPlanner:
        def __init__(self):
            self.round = 0

        def complete(self, messages, tools):
            self.round += 1
            calls = [
                ("get_robot_state", {}),
                ("list_robot_capabilities", {}),
                ("list_scene_objects", {}),
                ("get_object_pose", {"object_id": "temporary_crate"}),
                ("get_safe_approach_pose", {"object_id": "temporary_crate"}),
                ("get_robot_state", {}),
                ("list_robot_capabilities", {}),
            ]
            if self.round <= len(calls):
                return ComplexPlannerModel.call(*calls[self.round - 1]), {}
            return ComplexPlannerModel.call("finish_plan", {
                "steps": [{
                    "action": "navigate",
                    "destination_id": "temporary_crate",
                    "x": None,
                    "y": None,
                    "duration_seconds": None,
                    "description": "前往障碍箱安全接近点",
                }],
                "message": "已完成多轮观察并生成计划。",
            }), {}

    model = LongPlanner()
    plan = Supervisor(Store(tmp_path / "runs.db"), model).plan(
        "先查询现场和机器人能力，再去障碍箱旁边", True
    )
    assert model.round == 8
    assert plan["steps"][0]["destination_id"] == "temporary_crate"


def test_complex_plan_cannot_use_unqueried_object(tmp_path):
    finish = {
        "steps": [{
            "action": "navigate", "destination_id": "temporary_crate", "x": None, "y": None,
            "duration_seconds": None, "description": "前往障碍箱",
        }],
        "message": "执行。",
    }
    supervisor = Supervisor(Store(tmp_path / "runs.db"), ComplexPlannerModel(finish))
    with pytest.raises(ModelError, match="未经场景工具"):
        supervisor.plan("先去障碍箱检查，然后等待。", True)


def test_complex_planner_retries_protocol_once_without_rules_fallback(tmp_path):
    class FlakyPlanner(ComplexPlannerModel):
        def __init__(self):
            super().__init__()
            self.failed = False

        def complete(self, messages, tools):
            if not self.failed:
                self.failed = True
                raise ModelError("OpenFang response must contain valid gateway tool requests")
            return super().complete(messages, tools)

    model = FlakyPlanner()
    plan = Supervisor(Store(tmp_path / "runs.db"), model).plan(
        "先去货架 A，然后回起点等待。", True
    )
    assert plan["backend"] != "deterministic"
    assert plan["protocol_retries"] == 1
    assert plan["steps"]


def test_complex_planner_allows_two_protocol_repairs_without_rules_fallback(tmp_path):
    class TwiceFlakyPlanner(ComplexPlannerModel):
        def __init__(self):
            super().__init__()
            self.failures = 0

        def complete(self, messages, tools):
            if self.failures < 2:
                self.failures += 1
                raise ModelError("OpenFang response must contain valid gateway tool requests")
            return super().complete(messages, tools)

    model = TwiceFlakyPlanner()
    plan = Supervisor(Store(tmp_path / "runs.db"), model).plan(
        "先去货架 A，然后回起点等待。", True
    )
    assert plan["backend"] != "deterministic"
    assert plan["protocol_retries"] == 2
    assert plan["steps"]


def test_clarification_preserves_repair_ceiling_and_context(tmp_path):
    store = Store(tmp_path / "runs.db")
    supervisor = Supervisor(store, GroundedClarificationModel())
    parent = supervisor.plan("去未知地点", False)
    plan = supervisor.plan("货架 A", True, parent["id"])
    assert plan["steps"][0]["auto_repair"] is False
    assert plan["request_text"] == "去未知地点\n货架 A"
    assert plan["backend"] != "deterministic"
    assert store.get_plan(parent["id"])["status"] == "continued"


def test_recent_plans_are_ordered_by_creation(tmp_path):
    store = Store(tmp_path / "runs.db")
    store.save_plan({"id": "old", "created": 1, "status": "continued"})
    store.save_plan({"id": "new", "created": 2, "status": "dispatched"})
    assert [plan["id"] for plan in store.recent_plans()] == ["new", "old"]
