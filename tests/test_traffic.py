import threading
import time

from robot_debug_lab.traffic import FleetTrafficCoordinator


def test_crossing_routes_wait_and_release_without_partial_holds():
    traffic = FleetTrafficCoordinator(clearance=0.65)
    first = traffic.acquire(
        "robot_1", "run-1", "S1", [[-1.0, 0.0], [1.0, 0.0]],
        threading.Event(),
    )
    result = {}

    def request_crossing_route():
        result["reservation"] = traffic.acquire(
            "robot_2", "run-2", "S1", [[0.0, -1.0], [0.0, 1.0]],
            threading.Event(),
        )

    thread = threading.Thread(target=request_crossing_route)
    thread.start()
    time.sleep(0.12)
    assert thread.is_alive()
    assert traffic.snapshot()["queued"] == 1
    assert traffic.release(first["token"])
    thread.join(timeout=1)

    second = result["reservation"]
    assert second["blocked_by"] == ["robot_1"]
    assert second["wait_seconds"] >= 0.1
    assert traffic.release(second["token"])
    assert traffic.snapshot()["active"] == []


def test_spatially_separate_routes_can_be_active_together():
    traffic = FleetTrafficCoordinator(clearance=0.65)
    first = traffic.acquire(
        "robot_1", "run-1", "S1", [[-3.0, -2.0], [-2.0, -2.0]],
        threading.Event(),
    )
    second = traffic.acquire(
        "robot_2", "run-2", "S1", [[2.0, 2.0], [3.0, 2.0]],
        threading.Event(),
    )
    assert not first["blocked_by"]
    assert not second["blocked_by"]
    assert len(traffic.snapshot()["active"]) == 2
    traffic.release(first["token"])
    traffic.release(second["token"])


def test_waiting_route_can_be_canceled():
    traffic = FleetTrafficCoordinator()
    first = traffic.acquire(
        "robot_1", "run-1", "S1", None, threading.Event()
    )
    cancel = threading.Event()
    result = {}
    thread = threading.Thread(
        target=lambda: result.setdefault(
            "reservation",
            traffic.acquire("robot_2", "run-2", "S1", None, cancel),
        )
    )
    thread.start()
    time.sleep(0.05)
    cancel.set()
    thread.join(timeout=1)
    assert result["reservation"] is None
    assert traffic.snapshot()["queued"] == 0
    traffic.release(first["token"])


def test_waiting_route_returns_auditable_timeout_and_leaves_no_queue_entry():
    traffic = FleetTrafficCoordinator()
    first = traffic.acquire(
        "robot_1", "run-1", "S1", None, threading.Event()
    )

    timed_out = traffic.acquire(
        "robot_2", "run-2", "S1", None, threading.Event(),
        timeout_seconds=0.03,
    )

    assert timed_out["timed_out"] is True
    assert timed_out["blocked_by"] == ["robot_1"]
    assert timed_out["wait_seconds"] >= 0.03
    assert timed_out["timeout_seconds"] == 0.03
    assert traffic.snapshot()["queued"] == 0
    traffic.release(first["token"])
