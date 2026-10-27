import json
import threading

import pytest
from fastapi.testclient import TestClient

from robot_debug_lab.agents import ModelError
from robot_debug_lab.store import Store
from robot_debug_lab.supervisor import Supervisor
from robot_debug_lab.workflow import Workflow


def task(robot="robot_2", x=7.0, y=6.0, repair=True):
    return {"robot_id": robot, "x": x, "y": y, "auto_repair": repair}


class PlannerModel:
    def __init__(self, tasks):
        self.tasks = tasks

    def complete(self, messages, tools):
        return {"tool_calls": [{"function": {"name": "finish_plan", "arguments": json.dumps({
            "tasks": self.tasks, "message": "请明确目标" if not self.tasks else "执行导航",
        })}}]}, {"prompt_tokens": 12, "completion_tokens": 8}


@pytest.mark.parametrize("tasks", [
    [task(x=100)], [task(x=float("nan"))], [task(robot="robot_9")],
    [task(), task()], [dict(task(), shell="rm")], [dict(task(), auto_repair="true")],
])
def test_invalid_model_plan_fails_without_dispatch(tmp_path, tasks):
    store = Store(tmp_path / "runs.db")
    supervisor = Supervisor(store, PlannerModel(tasks))
    with pytest.raises(ModelError, match="计划无效"):
        supervisor.plan("去目标", True)
    assert store.all() == []
    assert not supervisor.lock.locked()


def test_repair_ceiling_and_plan_persistence(tmp_path):
    store = Store(tmp_path / "runs.db")
    supervisor = Supervisor(store, PlannerModel([task()]))
    plan = supervisor.plan("机器人 2 去 (7,6)", False)
    assert plan["tasks"][0]["auto_repair"] is False
    assert plan["usage"]["prompt_tokens"] == 12
    assert store.get_plan(plan["id"]) == plan


def test_missing_destination_returns_clarification(tmp_path):
    supervisor = Supervisor(Store(tmp_path / "runs.db"), PlannerModel([]))
    assert supervisor.plan("让机器人走一下", True)["status"] == "needs_clarification"


def test_clarification_preserves_context_and_cannot_replay(tmp_path):
    store = Store(tmp_path / "runs.db")
    supervisor = Supervisor(store, PlannerModel([]))
    parent = supervisor.plan("让机器人 2 移动", False)
    class FollowupModel(PlannerModel):
        def complete(self, messages, tools):
            request = json.loads(messages[1]["content"])
            assert request["conversation"][0]["content"] == "让机器人 2 移动"
            assert request["conversation"][-1]["content"] == "去 (7,6)"
            assert request["auto_repair_ceiling"] is False
            return super().complete(messages, tools)
    supervisor.model = FollowupModel([task()])
    plan = supervisor.plan("去 (7,6)", True, parent["id"])
    assert plan["tasks"][0]["auto_repair"] is False
    assert store.get_plan(parent["id"])["status"] == "continued"
    assert plan["request_text"] == "让机器人 2 移动\n去 (7,6)"
    with pytest.raises(ValueError, match="等待澄清"):
        supervisor.plan("再来一次", True, parent["id"])
    assert not supervisor.lock.locked()


def test_recent_plans_are_ordered_by_creation_not_updates(tmp_path):
    store = Store(tmp_path / "runs.db")
    store.save_plan({"id": "old", "created": 1, "status": "needs_clarification"})
    store.save_plan({"id": "new", "created": 2, "status": "dispatched"})
    store.save_plan({"id": "old", "created": 1, "status": "continued"})
    assert [p["id"] for p in store.recent_plans()] == ["new", "old"]


class FakeBridge:
    def __init__(self, healthy=False):
        self.locks = {r: threading.Lock() for r in ["robot_1", "robot_2"]}
        self.healthy = {r: healthy for r in self.locks}
        self.moves, self.repairs = [], []

    def navigate(self, rid, x, y, cancel=None):
        self.moves.append((rid, x, y))
        return {"success": self.healthy[rid], "target": [x, y], "error": 0.1 if self.healthy[rid] else 2.0}

    def snapshot(self, rid):
        return {"robot_id": rid, "nodes": [f"/{rid}/controller"]}

    def parameters(self, rid):
        return {"max_speed": 2.0 if self.healthy[rid] else 0.0, "command_topic": "turtle1/cmd_vel"}

    def repair(self, rid, cause):
        self.repairs.append(rid)
        self.healthy[rid] = True
        return {"cause": cause}


def test_healthy_navigation_skips_diagnosis(tmp_path):
    bridge = FakeBridge(healthy=True)
    flow = Workflow(bridge, Store(tmp_path / "runs.db"))
    r = flow.submit_many([task()], "rules", "go", plan={"id": "plan1"})[0]
    flow.pool.shutdown(wait=True)
    result = flow.store.get(r["id"])
    assert result["status"] == "verified"
    assert result["diagnosis"] is None
    assert bridge.moves == [("robot_2", 7.0, 6.0)]
    assert bridge.repairs == []
    assert result["plan"]["status"] == "dispatched"
    assert result["plan"]["run_ids"] == [r["id"]]


def test_cancel_navigation_skips_diagnosis_and_repair(tmp_path):
    entered = threading.Event()
    class WaitingBridge(FakeBridge):
        def navigate(self, rid, x, y, cancel=None):
            entered.set()
            assert cancel.wait(3)
            return {"success": False, "target": [x, y], "message": "canceled"}

        def stop(self, rid):
            pass

    bridge = WaitingBridge()
    flow = Workflow(bridge, Store(tmp_path / "runs.db"))
    r = flow.submit_many([task()], "rules", "go", plan={"id": "plan1"})[0]
    assert entered.wait(3)
    assert flow.cancel(r["id"])
    flow.pool.shutdown(wait=True)
    result = flow.store.get(r["id"])
    assert result["status"] == "canceled"
    assert result["diagnosis"] is None
    assert bridge.repairs == []
    assert not bridge.locks["robot_2"].locked()


def test_cancel_holds_robot_until_stop_finishes(tmp_path):
    entered, stopping, release_stop = threading.Event(), threading.Event(), threading.Event()
    class SlowStopBridge(FakeBridge):
        def navigate(self, rid, x, y, cancel=None):
            entered.set()
            assert cancel.wait(3)
            return {"success": False}

        def stop(self, rid):
            stopping.set()
            assert release_stop.wait(3)

    bridge = SlowStopBridge()
    flow = Workflow(bridge, Store(tmp_path / "runs.db"))
    run = flow.submit_many([task()], "rules", "go", plan={"id": "p"})[0]
    assert entered.wait(3)
    cancel_thread = threading.Thread(target=flow.cancel, args=(run["id"],))
    cancel_thread.start()
    try:
        assert stopping.wait(3)
        assert bridge.locks["robot_2"].locked()
    finally:
        release_stop.set()
        cancel_thread.join(3)
        flow.pool.shutdown(wait=True)
    assert not flow.cancel(run["id"])
    assert not bridge.locks["robot_2"].locked()


@pytest.mark.parametrize("repair,expected", [(True, "verified"), (False, "diagnosed")])
def test_failure_diagnosis_and_same_target_replay(tmp_path, repair, expected):
    bridge = FakeBridge()
    flow = Workflow(bridge, Store(tmp_path / "runs.db"))
    r = flow.submit_many([task(repair=repair)], "rules", "go", plan={"id": "plan1"})[0]
    flow.pool.shutdown(wait=True)
    result = flow.store.get(r["id"])
    assert result["status"] == expected
    assert result["diagnosis"]["cause"] == "zero_speed"
    assert bridge.moves == [("robot_2", 7.0, 6.0)] * (2 if repair else 1)
    assert bridge.repairs == (["robot_2"] if repair else [])
    assert not bridge.locks["robot_2"].locked()


def test_busy_peer_rejects_entire_batch(tmp_path):
    bridge = FakeBridge()
    flow = Workflow(bridge, Store(tmp_path / "runs.db"))
    bridge.locks["robot_2"].acquire()
    try:
        with pytest.raises(ValueError, match="robot_2"):
            flow.submit_many([task("robot_1"), task()], "rules", "go")
        assert bridge.moves == []
        assert flow.store.all() == []
        assert not bridge.locks["robot_1"].locked()
    finally:
        bridge.locks["robot_2"].release()
        flow.close()


def test_second_schedule_failure_does_not_start_first_robot(tmp_path, monkeypatch):
    bridge = FakeBridge()
    flow = Workflow(bridge, Store(tmp_path / "runs.db"))
    real_submit = flow.pool.submit
    count = 0
    def fail_second(*args):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("executor unavailable")
        return real_submit(*args)
    monkeypatch.setattr(flow.pool, "submit", fail_second)
    with pytest.raises(RuntimeError):
        flow.submit_many([task("robot_1"), task()], "rules", "go")
    flow.pool.shutdown(wait=True)
    assert bridge.moves == []
    assert all(r["status"] == "failed" for r in flow.store.all())
    assert all(not lock.locked() for lock in bridge.locks.values())
    assert not flow.cancel_events


def test_assistant_api_clarification_auth_and_custom_target(tmp_path, monkeypatch):
    from robot_debug_lab.app import app
    store = Store(tmp_path / "runs.db")
    bridge = FakeBridge(healthy=True)
    flow = Workflow(bridge, store)
    supervisor = Supervisor(store, PlannerModel([]))
    for name, value in {"token": "test-token", "store": store, "workflow": flow, "supervisor": supervisor}.items():
        monkeypatch.setattr(app.state, name, value, raising=False)
    # No lifespan: isolated API test must never start real ROS nodes.
    client = TestClient(app)
    assert client.post("/api/assistant/tasks", json={"prompt": "go"}).status_code == 403
    client.headers["X-Lab-Token"] = "test-token"
    response = client.post("/api/assistant/tasks", json={"prompt": "让机器人走一下"})
    assert response.json()["run_ids"] == []
    assert bridge.moves == []
    assert client.post("/api/assistant/tasks", json={"prompt": "  "}).status_code == 422
    supervisor.model = PlannerModel([task()])
    response = client.post("/api/assistant/tasks", json={"prompt": "让机器人 2 去 (7,6)"})
    assert response.status_code == 200
    result = response.json()
    flow.pool.shutdown(wait=True)
    assert bridge.moves == [("robot_2", 7.0, 6.0)]
    assert store.get(result["run_ids"][0])["status"] == "verified"
    assert client.get("/api/assistant/plans/" + result["plan"]["id"]).json()["status"] == "dispatched"
    client.close()
