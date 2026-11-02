"""Deterministic full-route reservations for the shared two-robot world."""

import math
import threading
import time
import uuid


class FleetTrafficCoordinator:
    """Serialize spatially conflicting paths without partial resource holds."""

    def __init__(self, clearance=0.65, grid=0.20):
        self.clearance = float(clearance)
        self.grid = float(grid)
        self.condition = threading.Condition()
        self.active = {}
        self.queued = {}
        self.sequence = 0

    def _cells(self, points):
        if not points:
            return None
        dense = []
        for start, end in zip(points, points[1:]):
            x1, y1 = map(float, start)
            x2, y2 = map(float, end)
            distance = math.hypot(x2 - x1, y2 - y1)
            pieces = max(1, math.ceil(distance / self.grid))
            dense.extend(
                (x1 + (x2 - x1) * index / pieces,
                 y1 + (y2 - y1) * index / pieces)
                for index in range(pieces)
            )
        dense.append(tuple(map(float, points[-1])))
        padding = self.clearance / 2.0
        radius = math.ceil(padding / self.grid)
        offsets = [
            (dx, dy)
            for dx in range(-radius, radius + 1)
            for dy in range(-radius, radius + 1)
            if math.hypot(dx * self.grid, dy * self.grid) <= padding
        ]
        cells = set()
        for x, y in dense:
            cx, cy = round(x / self.grid), round(y / self.grid)
            cells.update((cx + dx, cy + dy) for dx, dy in offsets)
        return frozenset(cells)

    @staticmethod
    def _conflicts(left, right):
        # Missing path geometry is fail-closed: reserve the whole traffic layer.
        return left is None or right is None or not left.isdisjoint(right)

    def acquire(
        self, robot_id, run_id, step_id, points, cancel,
        timeout_seconds=None,
    ):
        cells = self._cells(points)
        requested = time.monotonic()
        deadline = (
            requested + float(timeout_seconds)
            if timeout_seconds is not None else None
        )
        blockers_seen = set()
        with self.condition:
            self.sequence += 1
            ticket = self.sequence
            self.queued[ticket] = {
                "robot_id": robot_id,
                "run_id": run_id,
                "step_id": step_id,
                "cells": cells,
            }
            while True:
                if cancel.is_set():
                    self.queued.pop(ticket, None)
                    self.condition.notify_all()
                    return None
                blockers = [
                    item for item in self.active.values()
                    if self._conflicts(cells, item["cells"])
                ]
                blockers_seen.update(item["robot_id"] for item in blockers)
                older_conflict = any(
                    other_ticket < ticket
                    and self._conflicts(cells, item["cells"])
                    for other_ticket, item in self.queued.items()
                )
                if not blockers and not older_conflict:
                    token = uuid.uuid4().hex[:12]
                    self.queued.pop(ticket, None)
                    granted = time.monotonic()
                    record = {
                        "token": token,
                        "robot_id": robot_id,
                        "run_id": run_id,
                        "step_id": step_id,
                        "cells": cells,
                        "reservation_cells": len(cells) if cells is not None else None,
                        "path_mode": "nav2_geometry" if cells is not None else "global_fallback",
                        "wait_seconds": round(granted - requested, 3),
                        "blocked_by": sorted(blockers_seen),
                    }
                    self.active[token] = record
                    return {key: value for key, value in record.items() if key != "cells"}
                if deadline is not None and time.monotonic() >= deadline:
                    self.queued.pop(ticket, None)
                    self.condition.notify_all()
                    return {
                        "timed_out": True,
                        "robot_id": robot_id,
                        "run_id": run_id,
                        "step_id": step_id,
                        "reservation_cells": len(cells) if cells is not None else None,
                        "path_mode": (
                            "nav2_geometry" if cells is not None else "global_fallback"
                        ),
                        "wait_seconds": round(time.monotonic() - requested, 3),
                        "timeout_seconds": float(timeout_seconds),
                        "blocked_by": sorted(blockers_seen),
                    }
                wait_for = 0.1
                if deadline is not None:
                    wait_for = max(0.001, min(wait_for, deadline - time.monotonic()))
                self.condition.wait(wait_for)

    def release(self, token):
        with self.condition:
            removed = self.active.pop(token, None)
            self.condition.notify_all()
            return removed is not None

    def snapshot(self):
        with self.condition:
            return {
                "active": [
                    {key: value for key, value in item.items() if key != "cells"}
                    for item in self.active.values()
                ],
                "queued": len(self.queued),
                "clearance": self.clearance,
            }
