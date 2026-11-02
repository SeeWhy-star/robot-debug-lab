"""Deterministic safe-pose analysis over the Nav2 occupancy map."""

from hashlib import sha256
import math
from pathlib import Path
import re
import time

from .config import ROOT


MAP_YAML = ROOT / "simulation" / "maps" / "rdl_warehouse.yaml"


def _yaml_scalar(text, name):
    match = re.search(rf"(?m)^\s*{re.escape(name)}\s*:\s*(.+?)\s*$", text)
    if not match:
        raise ValueError(f"Nav2 map metadata is missing {name}")
    return match.group(1).strip()


def _read_pgm(path):
    data = path.read_bytes()
    index = 0

    def token():
        nonlocal index
        while index < len(data):
            if data[index:index + 1] == b"#":
                index = data.find(b"\n", index)
                if index < 0:
                    raise ValueError("invalid PGM comment")
            elif data[index] in b" \t\r\n":
                index += 1
            else:
                break
        start = index
        while index < len(data) and data[index] not in b" \t\r\n#":
            index += 1
        return data[start:index]

    if token() != b"P5":
        raise ValueError("Nav2 occupancy map must be a binary PGM")
    width, height, maximum = int(token()), int(token()), int(token())
    # Consume only the header separator: pixel values may themselves be whitespace.
    if data[index:index + 2] == b"\r\n":
        index += 2
    elif index < len(data) and data[index] in b" \t\r\n":
        index += 1
    else:
        raise ValueError("invalid PGM header separator")
    pixels = data[index:index + width * height]
    if maximum != 255 or len(pixels) != width * height:
        raise ValueError("Nav2 occupancy map has unsupported PGM data")
    return width, height, pixels


def map_fingerprint(map_yaml=MAP_YAML):
    map_yaml = Path(map_yaml)
    metadata = map_yaml.read_text()
    image = _yaml_scalar(metadata, "image").strip("'\"")
    return sha256(
        metadata.encode() + b"\0" + (map_yaml.parent / image).resolve().read_bytes()
    ).hexdigest()[:16]


def final_navigation_targets(assignments):
    """The pair must remain at the analyzed targets after every mission finishes."""
    targets = {}
    for assignment in assignments:
        last_navigation = next((
            step for step in reversed(assignment["steps"])
            if step["action"] in {"navigate", "return_to_start"}
        ), None)
        if last_navigation and not last_navigation.get("destination_id"):
            targets[assignment["robot_id"]] = (
                last_navigation.get("x"), last_navigation.get("y")
            )
    return targets


class SafeFleetPoseAnalyzer:
    """Select safe map poses, then require Nav2 to confirm both real routes."""

    OBJECTIVES = {"maximize_separation", "balanced_coverage", "rendezvous"}

    def __init__(self, bridge, map_yaml=MAP_YAML):
        self.bridge = bridge
        self.map_yaml = Path(map_yaml)

    def _map(self):
        metadata = self.map_yaml.read_text()
        image = _yaml_scalar(metadata, "image").strip("'\"")
        resolution = float(_yaml_scalar(metadata, "resolution"))
        origin = [
            float(value) for value in
            _yaml_scalar(metadata, "origin").strip("[]").split(",")
        ]
        if (
            not math.isfinite(resolution) or resolution <= 0
            or len(origin) != 3 or not all(map(math.isfinite, origin))
            or origin[2] != 0.0
            or _yaml_scalar(metadata, "mode") != "trinary"
            or _yaml_scalar(metadata, "negate") != "0"
        ):
            raise ValueError("Nav2 map metadata is invalid")
        free_threshold = float(_yaml_scalar(metadata, "free_thresh"))
        if not 0 < free_threshold < 1:
            raise ValueError("Nav2 map free threshold is invalid")
        image_path = (self.map_yaml.parent / image).resolve()
        width, height, pixels = _read_pgm(image_path)
        return {
            "width": width,
            "height": height,
            "pixels": pixels,
            "resolution": resolution,
            "origin": origin[:2],
            "image": image_path,
            "fingerprint": map_fingerprint(self.map_yaml),
            "free_threshold": free_threshold,
        }

    @staticmethod
    def _safe_cells(grid, clearance):
        width, height = grid["width"], grid["height"]
        free = {
            (column, row)
            for row in range(height)
            for column in range(width)
            if 1.0 - grid["pixels"][row * width + column] / 255.0
            < grid["free_threshold"]
        }
        unsafe = {
            (column, row)
            for row in range(height)
            for column in range(width)
            if (column, row) not in free
        }
        # Occupied cells represent areas rather than infinitesimal center points.
        margin = clearance + grid["resolution"] / math.sqrt(2)
        radius = math.ceil(margin / grid["resolution"])
        offsets = [
            (dx, dy)
            for dx in range(-radius, radius + 1)
            for dy in range(-radius, radius + 1)
            if math.hypot(dx, dy) * grid["resolution"] < margin
        ]
        inflated = set()
        for column, row in unsafe:
            inflated.update(
                (column + dx, row + dy)
                for dx, dy in offsets
                if 0 <= column + dx < width and 0 <= row + dy < height
            )
        interior = {
            cell for cell in free - inflated
            if min(cell[0] + 0.5, cell[1] + 0.5,
                   width - cell[0] - 0.5, height - cell[1] - 0.5)
            * grid["resolution"] >= clearance
        }
        return interior, unsafe

    @staticmethod
    def _world(grid, cell):
        column, row = cell
        return (
            grid["origin"][0] + (column + 0.5) * grid["resolution"],
            grid["origin"][1]
            + (grid["height"] - row - 0.5) * grid["resolution"],
        )

    @staticmethod
    def _clearance(grid, cell, unsafe):
        column, row = cell
        obstacle_distance = min((
            math.hypot(column - other_column, row - other_row)
            * grid["resolution"]
            for other_column, other_row in unsafe
        ), default=math.inf) - grid["resolution"] / math.sqrt(2)
        border_distance = min(
            column + 0.5, row + 0.5,
            grid["width"] - column - 0.5, grid["height"] - row - 0.5,
        ) * grid["resolution"]
        return min(obstacle_distance, border_distance)

    def _candidates(self, grid, clearance):
        safe, unsafe = self._safe_cells(grid, clearance)
        stride = max(1, round(0.40 / grid["resolution"]))
        candidates = []
        for cell in safe:
            if cell[0] % stride or cell[1] % stride:
                continue
            x, y = self._world(grid, cell)
            if -4.0 <= x <= 4.0 and -4.0 <= y <= 4.0:
                candidates.append((cell, (round(x, 3), round(y, 3))))
        if len(candidates) < 2:
            raise ValueError("占据地图中没有足够的安全候选位置")
        return sorted(candidates), unsafe

    @staticmethod
    def _rank_pairs(candidates, robot_poses, objective):
        ranked = []
        desired_meeting_separation = 0.8
        # Precompute distances to evaluate two-center coverage over all samples.
        distances = {
            left: {right: math.dist(left, right) for _, right in candidates}
            for _, left in candidates
        } if objective == "balanced_coverage" else {}
        for index, (_, left) in enumerate(candidates):
            for _, right in candidates[index + 1:]:
                separation = math.dist(left, right)
                direct_a = math.dist(robot_poses[0], left) + math.dist(robot_poses[1], right)
                direct_b = math.dist(robot_poses[0], right) + math.dist(robot_poses[1], left)
                direct_travel = min(direct_a, direct_b)
                if objective == "maximize_separation":
                    score = (separation, -direct_travel)
                elif objective == "balanced_coverage":
                    covering_radius = max(
                        min(distances[left][point], distances[right][point])
                        for _, point in candidates
                    )
                    score = (-covering_radius, -direct_travel)
                else:
                    if not 0.65 <= separation <= 1.0:
                        continue
                    score = (-direct_travel - abs(
                        separation - desired_meeting_separation
                    ), 0.0)
                ranked.append((score, separation, left, right, direct_a, direct_b))
        return sorted(ranked, reverse=True)[:12]

    def analyze(self, objective, clearance=0.45):
        if objective not in self.OBJECTIVES:
            raise ValueError("不支持的空间分析目标")
        clearance = float(clearance)
        if not 0.35 <= clearance <= 0.8:
            raise ValueError("安全间距必须位于 0.35 到 0.8 米之间")
        robots = self.bridge.public()
        if len(robots) != 2 or any(not item.get("pose") for item in robots):
            raise ValueError("缺少两台机器人的实时 Gazebo 位姿")
        if any(item.get("busy") for item in robots):
            raise ValueError("机器人正在执行任务，不能生成新的空间分工")
        if any(
            item.get("localization", {}).get("status") != "aligned"
            for item in robots
        ):
            raise ValueError("机器人定位未对齐，不能生成空间站位")
        robot_poses = [
            (float(item["pose"]["x"]), float(item["pose"]["y"]))
            for item in robots
        ]
        grid = self._map()
        candidates, unsafe = self._candidates(grid, clearance)
        ranked = self._rank_pairs(candidates, robot_poses, objective)
        path_cache = {}
        started = time.monotonic()
        checked_pairs = 0
        evidence = {
            "map": self.map_yaml.name,
            "map_fingerprint": grid["fingerprint"],
            "resolution": grid["resolution"],
            "sampling_spacing": 0.40,
            "safe_candidate_count": len(candidates),
            "pair_search_limit": 12,
            "planning_time_budget_seconds": 20.0,
            "route_validation": "Nav2 ComputePathToPose",
            "algorithm": "inflated_occupancy_pair_search_v2",
            "optimality": "bounded_sample_search_not_global_optimum",
            "metric": "euclidean_distance",
            "scope": "static_map_safe_positions_with_live_nav2_routes",
        }
        for _, separation, left, right, direct_a, direct_b in ranked:
            if time.monotonic() - started >= 20.0:
                break
            checked_pairs += 1
            orientations = [(left, right), (right, left)]
            if direct_a > direct_b:
                orientations.reverse()
            routes = []
            for targets in orientations:
                routes = []
                for robot, target in zip(robots, targets):
                    if time.monotonic() - started >= 20.0:
                        break
                    key = (robot["id"], target)
                    if key not in path_cache:
                        path_cache[key] = self.bridge.plan_path(
                            robot["id"], target[0], target[1], include_path=True
                        )
                    route = path_cache[key]
                    if not route.get("reachable"):
                        break
                    routes.append(route)
                if len(routes) == 2:
                    break
            if len(routes) != 2:
                continue
            cells = []
            for target in targets:
                cells.append(min(
                    candidates,
                    key=lambda item: math.dist(item[1], target),
                )[0])
            assignments = [
                {
                    "robot_id": robot["id"],
                    "x": target[0],
                    "y": target[1],
                    "path_length": route.get("path_length"),
                    "path_pose_count": route.get("pose_count"),
                    "map_clearance": round(
                        self._clearance(grid, cell, unsafe), 3
                    ),
                }
                for robot, target, route, cell in zip(robots, targets, routes, cells)
            ]
            return {
                "resolved": True,
                "objective": objective,
                "assignments": assignments,
                "metrics": {
                    "target_separation": round(separation, 3),
                    "total_nav2_path_length": round(sum(
                        float(route.get("path_length") or 0.0) for route in routes
                    ), 3),
                    "requested_clearance": clearance,
                    "sample_covering_radius": round(max(
                        min(math.dist(point, left), math.dist(point, right))
                        for _, point in candidates
                    ), 3),
                },
                "evidence": dict(
                    evidence,
                    checked_pair_count=checked_pairs,
                    nav2_query_count=len(path_cache),
                ),
            }
        return {
            "resolved": False,
            "objective": objective,
            "reason": "no_valid_pair_within_search_budget",
            "evidence": dict(
                evidence,
                checked_pair_count=checked_pairs,
                nav2_query_count=len(path_cache),
            ),
        }

