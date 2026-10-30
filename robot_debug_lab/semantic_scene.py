"""Authoritative semantic objects aligned with the Gazebo warehouse world.

Object poses describe entities in ``rdl_warehouse.sdf.xacro``.  Navigation
always uses the separately reviewed approach pose; obstacle centers are never
returned as goals.
"""

from copy import deepcopy
import re
import threading


class UnknownSceneObject(ValueError):
    pass


class SemanticScene:
    def __init__(self):
        self._lock = threading.RLock()
        self._runtime_poses = {}
        self._objects = {
            "shelf_a": {
                "object_id": "shelf_a",
                "gazebo_entity": "shelf_A",
                "label": "货架 A",
                "kind": "shelf",
                "pose": {"x": 3.2, "y": 0.5, "yaw": 0.0},
                "safe_approach_pose": {"x": 2.0, "y": 0.5, "yaw": 0.0},
                "approach_poses": [
                    {
                        "approach_id": "west",
                        "label": "货架 A 西侧默认接近点",
                        "pose": {"x": 2.0, "y": 0.5, "yaw": 0.0},
                        "default": True,
                    },
                    {
                        "approach_id": "southwest",
                        "label": "货架 A 西南备用接近点",
                        "pose": {"x": 2.0, "y": -1.0, "yaw": 0.7},
                        "default": False,
                    },
                ],
                "aliases": ["货架a", "货架 a", "a货架", "shelf a", "shelf_a"],
            },
            "temporary_crate": {
                "object_id": "temporary_crate",
                "gazebo_entity": "temporary_crate",
                "label": "临时障碍箱",
                "kind": "obstacle",
                "pose": {"x": 0.55, "y": -0.5, "yaw": 0.0},
                "safe_approach_pose": {"x": 0.55, "y": 0.5, "yaw": -1.57},
                "aliases": [
                    "临时障碍箱", "实体障碍箱", "障碍箱", "箱子", "temporary crate",
                    "temporary_crate", "crate",
                ],
            },
            "charging_station": {
                "object_id": "charging_station",
                "gazebo_entity": "charging_station",
                "label": "充电站",
                "kind": "station",
                "pose": {"x": -4.3, "y": -3.15, "yaw": 0.0},
                "safe_approach_pose": {"x": -4.0, "y": -3.15, "yaw": 3.14},
                "aliases": ["充电站", "充电桩", "充电点", "charging station", "charger"],
            },
            "start": {
                "object_id": "start",
                "gazebo_entity": None,
                "label": "起点",
                "kind": "waypoint",
                "pose": {"x": -2.0, "y": -0.5, "yaw": 0.0},
                "safe_approach_pose": {"x": -2.0, "y": -0.5, "yaw": 0.0},
                "aliases": ["起点", "出发点", "初始点", "start", "home"],
            },
        }
        self._aliases = {}
        for object_id, item in self._objects.items():
            for alias in [object_id, item["label"], *item["aliases"]]:
                self._aliases[self._normalize(alias)] = object_id

    @staticmethod
    def _normalize(value):
        return re.sub(r"[\s_\-]+", "", str(value).strip().casefold())

    def list_scene_objects(self):
        return [
            {
                "object_id": item["object_id"],
                "gazebo_entity": item["gazebo_entity"],
                "label": item["label"],
                "kind": item["kind"],
                "aliases": list(item["aliases"]),
            }
            for item in self._objects.values()
        ]

    def _get(self, object_id):
        try:
            return self._objects[object_id]
        except KeyError:
            raise UnknownSceneObject(f"未知场景对象：{object_id}") from None

    def get_object_pose(self, object_id):
        item = self._get(object_id)
        with self._lock:
            runtime = deepcopy(self._runtime_poses.get(object_id))
        return {
            "object_id": object_id,
            "gazebo_entity": item["gazebo_entity"],
            "pose": runtime["pose"] if runtime else deepcopy(item["pose"]),
            "source": "gazebo_runtime_override" if runtime else "semantic_scene_registry",
            "navigation_goal": False,
        }

    def get_safe_approach_pose(self, object_id):
        item = self._get(object_id)
        approach = self.get_approach_options(object_id)[0]
        return {
            "object_id": object_id,
            "approach_id": approach["approach_id"],
            "pose": deepcopy(approach["pose"]),
            "source": "semantic_scene_registry",
            "navigation_goal": True,
        }

    def get_approach_options(self, object_id):
        item = self._get(object_id)
        with self._lock:
            runtime = deepcopy(self._runtime_poses.get(object_id))
        if runtime:
            return [{
                "approach_id": "runtime_safe",
                "label": f"{item['label']}当前安全接近点",
                "pose": runtime["safe_approach_pose"],
                "default": True,
            }]
        approaches = item.get("approach_poses") or [{
            "approach_id": "default",
            "label": f"{item['label']}安全接近点",
            "pose": item["safe_approach_pose"],
            "default": True,
        }]
        return deepcopy(approaches)

    def get_approach_pose(self, object_id, approach_id):
        for approach in self.get_approach_options(object_id):
            if approach["approach_id"] == approach_id:
                return {
                    "object_id": object_id,
                    "approach_id": approach_id,
                    "pose": deepcopy(approach["pose"]),
                    "source": "semantic_scene_registry",
                    "navigation_goal": True,
                }
        raise UnknownSceneObject(f"对象 {object_id} 没有接近点：{approach_id}")

    def set_runtime_pose(self, object_id, pose, safe_approach_pose):
        self._get(object_id)
        with self._lock:
            self._runtime_poses[object_id] = {
                "pose": deepcopy(pose),
                "safe_approach_pose": deepcopy(safe_approach_pose),
            }

    def clear_runtime_poses(self):
        with self._lock:
            self._runtime_poses.clear()

    def resolve_destination(self, name):
        normalized = self._normalize(name)
        object_id = self._aliases.get(normalized)
        if object_id is None:
            # Spatial suffixes still describe a destination.  Operational
            # verbs such as 检查/等待/休息 deliberately stay out of this list:
            # they change the requested mission and must be planned instead
            # of being silently discarded by destination resolution.
            suffixes = ("旁边", "附近", "那里", "处", "点")
            for suffix in suffixes:
                trimmed = self._normalize(re.sub(f"{suffix}$", "", str(name).strip()))
                if trimmed in self._aliases:
                    object_id = self._aliases[trimmed]
                    break
        if object_id is None:
            return {"resolved": False, "query": name, "reason": "unknown_object"}
        item = self._objects[object_id]
        approach = self.get_safe_approach_pose(object_id)
        return {
            "resolved": True,
            "query": name,
            "object_id": object_id,
            "label": item["label"],
            "kind": item["kind"],
            "pose": deepcopy(approach["pose"]),
            "pose_kind": "safe_approach",
        }

    def navigation_pose(self, object_id, approach_id=None):
        pose = (
            self.get_approach_pose(object_id, approach_id)["pose"]
            if approach_id
            else self.get_safe_approach_pose(object_id)["pose"]
        )
        return float(pose["x"]), float(pose["y"])


SCENE = SemanticScene()
