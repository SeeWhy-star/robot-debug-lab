import json
import math
import threading
import time
import pytest

from robot_debug_lab.fleet import FleetSupervisor
from robot_debug_lab.semantic_scene import SemanticScene
from robot_debug_lab.spatial import SafeFleetPoseAnalyzer, map_fingerprint, _read_pgm
from robot_debug_lab.store import Store
from robot_debug_lab.supervisor import Supervisor
from robot_debug_lab.workflow import Workflow


class SpatialBridge:
    robot_ids = ("robot_1", "robot_2")

    def __init__(self):
        self.locks = {rid: threading.Lock() for rid in self.robot_ids}
        self.poses = {
            "robot_1": {"x": -2.0, "y": -0.5},
            "robot_2": {"x": -2.0, "y": 0.5},
        }

    def public(self):
        return [
            {
                "id": rid,
                "pose": dict(
                    self.poses[rid], source="gazebo_odometry", observed=time.time()
                ),
                "busy": self.locks[rid].locked(),
                "scan": {"raw_scan_hz": 5.0, "nav2_scan_hz": 5.0},
                "localization": {"status": "aligned", "position_error": 0.02},
            }
            for rid in self.robot_ids
        ]

    def plan_path(self, robot, x, y, include_path=False):
        start = self.poses[robot]
        result = {
            "reachable": True,
            "target": [x, y],
            "path_length": round(math.hypot(x - start["x"], y - start["y"]), 3),
            "pose_count": 8,
        }
        if include_path:
            result["path"] = [[start["x"], start["y"]], [x, y]]
        return result

    def navigate(self, robot, x, y, cancel=None):
        self.poses[robot] = {"x": x, "y": y}
        return {
            "success": True,
            "error": 0.0,
            "elapsed": 0.01,
            "target": [x, y],
            "pose": dict(self.poses[robot]),
            "message": "4",
        }

    def stop(self, robot):
        return None


class SpatialFleetModel:
    def __init__(self):
        self.round = 0

    @staticmethod
    def call(name, args):
        return {
            "id": f"{name}-{time.monotonic_ns()}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }

    def complete(self, messages, tools):
        self.round += 1
        if self.round == 1:
            calls = [self.call("get_fleet_state", {})]
        elif self.round == 2:
            calls = [self.call("analyze_fleet_poses", {
                "objective": "maximize_separation", "clearance": 0.45,
            })]
        else:
            result = json.loads(next(
                item["content"] for item in reversed(messages)
                if item["role"] == "tool"
            ))
            calls = [self.call("finish_fleet_plan", {
                "assignments": [
                    {
                        "robot_id": item["robot_id"],
                        "steps": [{
                            "action": "navigate",
                            "destination_id": None,
                            "x": item["x"],
                            "y": item["y"],
                            "duration_seconds": None,
                            "description": "前往空间分析生成的安全站位",
                        }],
                        "rationale": "保持空间工具生成的机器人目标绑定",
                    }
                    for item in result["assignments"]
                ],
                "message": "已根据占据地图和Nav2真实路径规划最大安全分离任务。",
            })]
        return {"role": "assistant", "tool_calls": calls}, {
            "prompt_tokens": 10, "completion_tokens": 5,
        }


def test_safe_pose_analyzer_supports_three_map_grounded_objectives():
    bridge = SpatialBridge()
    analyzer = SafeFleetPoseAnalyzer(bridge)

    separated = analyzer.analyze("maximize_separation", 0.45)
    covered = analyzer.analyze("balanced_coverage", 0.45)
    rendezvous = analyzer.analyze("rendezvous", 0.45)

    assert separated["resolved"] and covered["resolved"] and rendezvous["resolved"]
    assert separated["metrics"]["target_separation"] > 9.0
    assert covered["metrics"]["sample_covering_radius"] < separated["metrics"]["sample_covering_radius"]
    assert 0.65 <= rendezvous["metrics"]["target_separation"] <= 1.0
    assert separated["evidence"]["map_fingerprint"] == map_fingerprint()
    assert separated["evidence"]["optimality"] == "bounded_sample_search_not_global_optimum"
    assert all(
        item["map_clearance"] >= 0.45
        for result in (separated, covered, rendezvous)
        for item in result["assignments"]
    )


def test_fleet_agent_uses_only_spatial_tool_robot_bound_coordinates(tmp_path):
    store = Store(tmp_path / "spatial.sqlite")
    bridge = SpatialBridge()
    mission = Supervisor(store, scene=SemanticScene())
    supervisor = FleetSupervisor(
        store, bridge, mission, SpatialFleetModel()
    )

    plan = supervisor.plan("让两个机器人找到彼此之间的最远安全距离")

    assert plan["status"] == "ready"
    assert plan["spatial_analysis"]["objective"] == "maximize_separation"
    assert plan["allow_handoff"] is False
    assert [item["name"] for item in plan["tool_trace"]] == [
        "get_fleet_state", "analyze_fleet_poses",
    ]
    expected = {
        item["robot_id"]: (item["x"], item["y"])
        for item in plan["spatial_analysis"]["assignments"]
    }
    actual = {
        item["robot_id"]: (item["steps"][0]["x"], item["steps"][0]["y"])
        for item in plan["assignments"]
    }
    assert actual == expected


def test_spatial_fleet_execution_records_fresh_pair_acceptance(tmp_path):
    store = Store(tmp_path / "spatial-workflow.sqlite")
    bridge = SpatialBridge()
    mission = Supervisor(store, scene=SemanticScene())
    supervisor = FleetSupervisor(
        store, bridge, mission, SpatialFleetModel()
    )
    plan = supervisor.plan("让两个机器人找到彼此之间的最远安全距离")
    workflow = Workflow(bridge, store)
    try:
        runs = workflow.submit_fleet(plan)
        deadline = time.monotonic() + 3
        acceptance = None
        while time.monotonic() < deadline:
            acceptance = store.get_plan(plan["id"]).get("fleet_acceptance")
            if acceptance:
                break
            time.sleep(0.02)

        assert [store.get(run["id"])["status"] for run in runs] == [
            "verified", "verified",
        ]
        assert acceptance["success"] is True
        assert acceptance["source"] == "gazebo_ground_truth"
        assert acceptance["actual_separation"] == acceptance["target_separation"]
        assert acceptance["robot_target_errors"] == {
            "robot_1": 0.0, "robot_2": 0.0,
        }
    finally:
        workflow.close()


def test_pgm_binary_pixels_and_metadata_fingerprint(tmp_path):
    image = tmp_path / "test.pgm"
    image.write_bytes(b"P5\n# test\n2 2\n255\n" + bytes([10, 32, 0, 254]))
    assert _read_pgm(image) == (2, 2, bytes([10, 32, 0, 254]))
    metadata = tmp_path / "test.yaml"
    metadata.write_text("image: test.pgm\nresolution: 0.05\n")
    before = map_fingerprint(metadata)
    metadata.write_text("image: test.pgm\nresolution: 0.10\n")
    assert map_fingerprint(metadata) != before


def test_spatial_analysis_returns_no_targets_when_nav2_cannot_reach_any_pair():
    bridge = SpatialBridge()
    bridge.plan_path = lambda *args, **kwargs: {"reachable": False}

    result = SafeFleetPoseAnalyzer(bridge).analyze("maximize_separation")

    assert result["resolved"] is False
    assert "assignments" not in result
    assert result["reason"] == "no_valid_pair_within_search_budget"
    assert result["evidence"]["checked_pair_count"] <= 12
    assert result["evidence"]["nav2_query_count"] <= 48


@pytest.mark.parametrize("mutation", ["swap", "return_home"])
def test_spatial_plan_rejects_swapped_binding_or_later_navigation(tmp_path, mutation):
    class BadSpatialModel(SpatialFleetModel):
        def complete(self, messages, tools):
            message, usage = super().complete(messages, tools)
            if self.round == 3:
                function = message["tool_calls"][0]["function"]
                args = json.loads(function["arguments"])
                if mutation == "swap":
                    left, right = args["assignments"]
                    left["steps"], right["steps"] = right["steps"], left["steps"]
                else:
                    args["assignments"][0]["steps"].append({
                        "action": "return_to_start", "description": "再回起点",
                    })
                function["arguments"] = json.dumps(args)
            return message, usage

    store = Store(tmp_path / "unsafe-spatial.sqlite")
    bridge = SpatialBridge()
    supervisor = FleetSupervisor(
        store, bridge, Supervisor(store, scene=SemanticScene()), BadSpatialModel()
    )
    with pytest.raises(Exception, match="坐标|目标分工"):
        supervisor.plan("让两个机器人尽量远离")


def test_spatial_acceptance_rejects_stale_or_localization_pose(tmp_path):
    store = Store(tmp_path / "stale.sqlite")
    bridge = SpatialBridge()
    plan = {
        "id": "test-pair", "created": time.time(), "status": "dispatched",
        "spatial_analysis": {
            "resolved": True,
            "assignments": [
                {"robot_id": rid, "x": pose["x"], "y": pose["y"]}
                for rid, pose in bridge.poses.items()
            ],
        },
    }
    original_public = bridge.public
    workflow = Workflow(bridge, store)
    try:
        for source, observed in [
            ("gazebo_odometry", time.time() - 20), ("amcl", time.time()),
        ]:
            def stale_public():
                robots = original_public()
                for robot in robots:
                    robot["pose"].update(source=source, observed=observed)
                return robots
            bridge.public = stale_public
            workflow._record_spatial_acceptance(plan, [])
            assert plan["fleet_acceptance"]["success"] is False
            assert plan["fleet_acceptance"]["reason"] == "missing_or_stale_gazebo_pose"
    finally:
        workflow.close()

