import threading, concurrent.futures, time
from copy import deepcopy
from .agents import RobotAgent, Canceled, infer
from .config import robot_id, ROBOTS, AGENT_BACKEND
from .supervisor import NavigationTask


class Workflow:
    def __init__(self, bridge, store):
        self.bridge = bridge
        self.store = store
        self.agents = {rid: RobotAgent(bridge, store, robot=rid) for rid in ROBOTS}
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.cancel_events = {}
        self.dispatch_lock = threading.Lock()
        self.closed = False

    def submit(self, rid, mode, prompt, repair=True):
        return self.submit_many([
            {"robot_id": robot_id(rid), "x": 8.0, "y": 8.0, "auto_repair": repair}
        ], mode, prompt)[0]

    def submit_many(self, tasks, mode, prompt, plan=None):
        tasks = [NavigationTask.model_validate(t) for t in tasks]
        ids = [t.robot_id for t in tasks]
        if not 1 <= len(tasks) <= 2 or len(set(ids)) != len(ids):
            raise ValueError("每台机器人只能有一个目标")
        acquired, runs = [], []
        gate = threading.Event()
        committed = False

        def start(r, repair, cancel):
            gate.wait()
            if committed:
                self.run(r, repair, cancel)

        # Reserve the entire batch before scheduling: a busy peer must not
        # leave one robot moving from a partially accepted request.
        with self.dispatch_lock:
            try:
                if self.closed:
                    raise ValueError("服务正在关闭")
                for rid in sorted(ids):
                    lock = self.bridge.locks[rid]
                    if not lock.acquire(False):
                        raise ValueError(f"{rid} 正在执行任务，请等待或取消后重试")
                    acquired.append(lock)
                for task in tasks:
                    r = self.store.new(task.robot_id, mode, prompt)
                    runs.append(r)
                    r.update(backend="rules" if mode == "rules" else AGENT_BACKEND,
                             target=[task.x, task.y],
                             task_type="navigation" if plan else "diagnosis")
                    if plan:
                        r["plan"] = deepcopy(plan)
                    self.store.save(r)
                    cancel = threading.Event()
                    self.cancel_events[r["id"]] = cancel
                    self.pool.submit(start, r, task.auto_repair, cancel)
                if plan:
                    run_ids = [r["id"] for r in runs]
                    for r in runs:
                        r["plan"].update(status="dispatched", run_ids=run_ids)
                        self.store.save(r)
                committed = True
                return runs
            except Exception:
                for r in runs:
                    self.cancel_events.pop(r["id"], None)
                    r.update(status="failed", error="任务批次未提交，未执行动作", finished=time.time())
                    try:
                        self.store.save(r)
                    except Exception:
                        pass
                for lock in acquired:
                    lock.release()
                raise
            finally:
                gate.set()

    def run(self, r, repair, cancel):
        rid = r["robot_id"]
        try:
            self.store.event(
                r,
                "assistant",
                f"Assistant → {rid} Agent",
                {"scope": rid, "mode": r["mode"], "target": r["target"],
                 "plan_id": r.get("plan", {}).get("id")},
            )
            if cancel.is_set():
                raise Canceled()
            if r.get("plan"):
                self.store.event(r, "plan", "Assistant 任务分派", r["plan"])
            r["status"] = "navigating" if r["task_type"] == "navigation" else "reproducing"
            self.store.save(r)
            r["baseline"] = self.bridge.navigate(rid, *r["target"], cancel=cancel)
            self.store.event(r, "experiment", "首次导航" if r["task_type"] == "navigation" else "原始任务复现", r["baseline"])
            if cancel.is_set():
                raise Canceled()
            if r["task_type"] == "navigation" and r["baseline"]["success"]:
                r["validation"] = r["baseline"]
                r["status"] = "verified"
                self.store.event(r, "validation", "首次导航位置验收通过，无需修复", r["validation"])
                return
            r["status"] = "investigating"
            self.store.save(r)
            diagnosis = self.agents[rid].diagnose(r, cancel)
            r["diagnosis"] = diagnosis
            self.store.event(r, "diagnosis", "故障判断与证据", diagnosis)
            if cancel.is_set():
                raise Canceled()
            if r["baseline"]["success"] and diagnosis["cause"] == "healthy":
                r["validation"] = r["baseline"]
                r["status"] = "verified"
            elif not repair:
                r["status"] = "diagnosed"
            elif diagnosis["cause"] in ["healthy", "unknown"]:
                r["status"] = "unresolved"
            else:
                # The model proposes; a fresh observation guards the concrete mutation.
                current = self.bridge.snapshot(rid)
                params = self.bridge.parameters(rid)
                if cancel.is_set():
                    raise Canceled()
                if infer(current, params) != diagnosis["cause"]:
                    r["status"] = "unresolved"
                    self.store.event(r, "guard", "修复未执行：当前证据与建议不符")
                else:
                    r["status"] = "repairing"
                    self.store.save(r)
                    result = self.bridge.repair(rid, diagnosis["cause"])
                    self.store.event(r, "repair", "执行仿真修复", result)
                    if cancel.is_set():
                        raise Canceled()
                    r["status"] = "validating"
                    self.store.save(r)
                    r["validation"] = self.bridge.navigate(rid, *r["target"], cancel=cancel)
                    self.store.event(r, "validation", "独立位置验收", r["validation"])
                    r["status"] = (
                        "verified" if r["validation"]["success"] else "unresolved"
                    )
        except Canceled:
            r["status"] = "canceled"
            self.store.event(r, "cancel", "用户取消任务")
        except Exception as e:
            r["status"] = "failed"
            r["error"] = str(e)[:500]
            self.store.event(r, "error", "任务未完成", {"error": r["error"]})
        finally:
            with self.dispatch_lock:
                if cancel.is_set():
                    r["status"] = "canceled"
                r["finished"] = time.time()
                try:
                    self.store.save(r)
                finally:
                    self.cancel_events.pop(r["id"], None)
                    self.bridge.locks[rid].release()

    def cancel(self, rid):
        # Keep completion/reassignment blocked until the old task is stopped.
        # Otherwise a delayed cancel could stop a newer task on the same robot.
        with self.dispatch_lock:
            event = self.cancel_events.get(rid)
            if not event:
                return False
            event.set()
            r = self.store.get(rid)
            self.bridge.stop(r["robot_id"])
            return True

    def close(self):
        with self.dispatch_lock:
            self.closed = True
            for e in list(self.cancel_events.values()):
                e.set()
        self.pool.shutdown(wait=True)
