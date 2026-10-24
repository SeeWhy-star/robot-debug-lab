import threading, concurrent.futures, time
from .agents import RobotAgent, Canceled, infer
from .config import robot_id


class Workflow:
    def __init__(self, bridge, store):
        self.bridge = bridge
        self.store = store
        self.agent = RobotAgent(bridge, store)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.cancel_events = {}

    def submit(self, rid, mode, prompt, repair=True):
        robot_id(rid)
        if not self.bridge.locks[rid].acquire(False):
            raise ValueError("Robot already has an active task")
        r = self.store.new(rid, mode, prompt)
        cancel = threading.Event()
        self.cancel_events[r["id"]] = cancel
        self.pool.submit(self.run, r, repair, cancel)
        return r

    def run(self, r, repair, cancel):
        rid = r["robot_id"]
        try:
            self.store.event(
                r,
                "assistant",
                f"Assistant → {rid} Agent",
                {"scope": rid, "mode": r["mode"]},
            )
            r["status"] = "reproducing"
            self.store.save(r)
            r["baseline"] = self.bridge.navigate(rid, cancel=cancel)
            self.store.event(r, "experiment", "原始任务复现", r["baseline"])
            if cancel.is_set():
                raise Canceled()
            r["status"] = "investigating"
            self.store.save(r)
            diagnosis = self.agent.diagnose(r, cancel)
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
                    r["validation"] = self.bridge.navigate(rid, cancel=cancel)
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
            if cancel.is_set():
                r["status"] = "canceled"
            r["finished"] = time.time()
            self.store.save(r)
            self.cancel_events.pop(r["id"], None)
            self.bridge.locks[rid].release()

    def cancel(self, rid):
        event = self.cancel_events.get(rid)
        if not event:
            return False
        event.set()
        r = self.store.get(rid)
        self.bridge.stop(r["robot_id"])
        return True

    def close(self):
        for e in list(self.cancel_events.values()):
            e.set()
        self.pool.shutdown(wait=True)
