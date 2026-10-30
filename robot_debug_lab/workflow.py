import threading, concurrent.futures, time
from copy import deepcopy
from .agents import RobotAgent, Canceled, infer
from .config import robot_id, ROBOTS, AGENT_BACKEND
from .supervisor import NavigationTask


class Workflow:
    def __init__(self, bridge, store, replanner=None):
        self.bridge = bridge
        self.store = store
        self.replanner = replanner
        robot_ids = tuple(getattr(bridge, "robot_ids", bridge.locks.keys()))
        self.agents = {rid: RobotAgent(bridge, store, robot=rid) for rid in robot_ids}
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.cancel_events = {}
        self.dispatch_lock = threading.Lock()
        self.closed = False

    def submit(self, rid, mode, prompt, repair=True):
        return self.submit_many([
            {"robot_id": robot_id(rid), "x": 2.0, "y": 0.5, "auto_repair": repair}
        ], mode, prompt)[0]

    def submit_many(self, tasks, mode, prompt, plan=None):
        tasks = [NavigationTask.model_validate(t) for t in tasks]
        ids = [t.robot_id for t in tasks]
        if any(rid not in self.bridge.locks for rid in ids):
            raise ValueError("任务包含当前仿真中不存在的机器人")
        if len(tasks) != 1 or len(set(ids)) != len(ids):
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

    def submit_mission(self, plan, mode="ai"):
        """Schedule one ordered mission while holding the robot reservation."""
        steps = deepcopy(plan.get("steps") or [])
        if not steps or len(steps) > 8:
            raise ValueError("任务必须包含 1 到 8 个步骤")
        for step in steps:
            if step.get("action") not in {
                "navigate", "wait", "return_to_start", "enter_idle", "simulate_charge"
            }:
                raise ValueError("任务包含不支持的步骤")
            if step.get("action") in {"navigate", "return_to_start"}:
                NavigationTask.model_validate({
                    "robot_id": "robot_1", "x": step.get("x"), "y": step.get("y"),
                    "auto_repair": bool(step.get("auto_repair")),
                })
        with self.dispatch_lock:
            if self.closed:
                raise ValueError("服务正在关闭")
            lock = self.bridge.locks["robot_1"]
            if not lock.acquire(False):
                raise ValueError("robot_1 正在执行任务，请等待或取消后重试")
            try:
                r = self.store.new("robot_1", mode, plan["request_text"])
                r.update(
                    backend=plan.get("backend", AGENT_BACKEND),
                    planner_backend=plan.get("backend", AGENT_BACKEND),
                    task_type="mission",
                    plan=deepcopy(plan),
                    steps=steps,
                    current_step=None,
                    active_target=None,
                    target=None,
                    target_history=[],
                    plan_version=1,
                    plan_revisions=[],
                )
                self.store.save(r)
                cancel = threading.Event()
                self.cancel_events[r["id"]] = cancel
                self.pool.submit(self.run_mission, r, cancel)
                return r
            except Exception:
                self.cancel_events.pop(r["id"], None) if "r" in locals() else None
                lock.release()
                raise

    def _mission_navigation(self, r, step, cancel):
        rid = r["robot_id"]
        target = [step["x"], step["y"]]
        r.update(status="navigating", active_target=target, target=target)
        self.store.save(r)
        baseline = self.bridge.navigate(rid, *target, cancel=cancel)
        step.setdefault("attempts", []).append({
            "target": target, "approach_id": step.get("approach_id"),
            "result": baseline,
        })
        step["baseline"] = baseline
        r["baseline"] = baseline
        self.store.event(r, "experiment", f"{step['id']} 导航尝试", baseline)
        if cancel.is_set():
            raise Canceled()
        if baseline["success"]:
            step["validation"] = baseline
            r["validation"] = baseline
            return True

        r["status"] = "investigating"
        self.store.save(r)
        # Known ROS/Nav2 mechanisms have deterministic, freshly observed
        # signatures.  If all of them are healthy, route recovery is the next
        # problem; spending an LLM loop repeatedly proving health adds no value.
        current = self.bridge.snapshot(rid)
        params = self.bridge.parameters(rid)
        preflight_cause = infer(current, params)
        if preflight_cause == "healthy":
            evidence = self.store.event(
                r, "route_preflight", f"{step['id']} 导航栈健康检查", {
                    "topics": current.get("topics"),
                    "nav2_lifecycle": params.get("nav2_lifecycle"),
                    "controller": params.get("controller"),
                    "navigation_failure": baseline,
                }
            )
            diagnosis = {
                "cause": "healthy",
                "summary": "雷达链路、Nav2 lifecycle 和控制器参数均健康，转入路线恢复。",
                "evidence_ids": [evidence["id"]],
                "repair_rationale": "不执行系统修复；查询审核过的替代接近点。",
                "source": "deterministic_preflight",
            }
            step["diagnosis"] = diagnosis
            r["diagnosis"] = diagnosis
            self.store.event(r, "diagnosis", f"{step['id']} 系统故障前置排除", diagnosis)
            step["failure_reason"] = "导航栈健康，但当前语义接近点实测不可达"
            return False
        diagnosis = self.agents[rid].diagnose(r, cancel)
        step["diagnosis"] = diagnosis
        r["diagnosis"] = diagnosis
        self.store.event(r, "diagnosis", f"{step['id']} 故障判断与证据", diagnosis)
        if cancel.is_set():
            raise Canceled()
        if not step.get("auto_repair", False) or diagnosis["cause"] in {"healthy", "unknown"}:
            step["failure_reason"] = "故障未获准修复或证据不足"
            return False
        current = self.bridge.snapshot(rid)
        params = self.bridge.parameters(rid)
        if infer(current, params) != diagnosis["cause"]:
            step["failure_reason"] = "当前证据与修复建议不一致"
            self.store.event(r, "guard", "修复未执行：当前证据与建议不符")
            return False
        r["status"] = "repairing"
        self.store.save(r)
        repair = self.bridge.repair(rid, diagnosis["cause"])
        step["repair"] = repair
        self.store.event(r, "repair", f"{step['id']} 执行白名单修复", repair)
        if cancel.is_set():
            raise Canceled()
        r["status"] = "validating"
        self.store.save(r)
        validation = self.bridge.navigate(rid, *target, cancel=cancel)
        step["validation"] = validation
        r["validation"] = validation
        self.store.event(r, "validation", f"{step['id']} Gazebo 独立位置验收", validation)
        if not validation["success"]:
            step["failure_reason"] = "修复后重放未通过位置验收"
        return validation["success"]

    def _replan_failed_navigation(self, r, index, step, cancel):
        if (
            self.replanner is None
            or r.get("mode") != "ai"
            or (step.get("diagnosis") or {}).get("cause") != "healthy"
            or len(r.get("plan_revisions", [])) >= 2
        ):
            return False
        r["status"] = "replanning"
        self.store.event(r, "replan_started", f"{step['id']} 启动任务级重规划", {
            "failed_target": [step.get("x"), step.get("y")],
            "failed_approach_id": step.get("approach_id"),
            "preserved_completed_steps": [
                item["id"] for item in r["steps"][:index]
                if item.get("status") == "completed"
            ],
        })
        revision = self.replanner.replan(r, index, cancel)
        if not revision or revision["decision"] == "abort":
            if revision:
                step["failure_reason"] = revision["reason"]
                r["plan_revisions"].append({
                    "version": r["plan_version"], **revision,
                })
            return False
        replacement = revision.pop("replacement_step")
        attempts = step.get("attempts", [])
        original = {
            key: step.get(key)
            for key in ["id", "destination_id", "approach_id", "x", "y", "description"]
        }
        step.update(replacement)
        step["attempts"] = attempts
        step["status"] = "running"
        r["plan_version"] += 1
        record = {
            "version": r["plan_version"],
            "failed_step_id": step["id"],
            "original": original,
            "replacement": {
                key: step.get(key)
                for key in ["id", "destination_id", "approach_id", "x", "y", "description"]
            },
            **revision,
        }
        r["plan_revisions"].append(record)
        self.store.event(r, "replan_applied", f"{step['id']} 应用受限重规划", record)
        self.store.save(r)
        return self._mission_navigation(r, step, cancel)

    def run_mission(self, r, cancel):
        rid = r["robot_id"]
        try:
            self.store.event(r, "plan", "结构化多步骤任务开始", r["plan"])
            for index, step in enumerate(r["steps"]):
                if cancel.is_set():
                    raise Canceled()
                r["current_step"] = index
                step.update(status="running", started=time.time(), failure_reason=None)
                self.store.event(
                    r, "step_started", f"{step['id']} 开始：{step['description']}",
                    {"step_id": step["id"], "action": step["action"]},
                )
                action = step["action"]
                if action in {"navigate", "return_to_start"}:
                    success = self._mission_navigation(r, step, cancel)
                    if not success and action == "navigate":
                        success = self._replan_failed_navigation(
                            r, index, step, cancel
                        )
                    if not success:
                        step["status"] = "failed"
                        r["status"] = "unresolved"
                        self.store.event(r, "step_failed", f"{step['id']} 失败", {
                            "reason": step["failure_reason"]
                        })
                        break
                    r["target_history"].append({
                        "step_id": step["id"], "target": [step["x"], step["y"]],
                        "finished": time.time(),
                    })
                    r["active_target"] = None
                    self.store.save(r)
                elif action == "wait":
                    r["status"] = "waiting"
                    self.store.save(r)
                    if cancel.wait(step["duration_seconds"]):
                        raise Canceled()
                elif action == "simulate_charge":
                    r["status"] = "simulated_charging"
                    self.store.event(r, "charging_started", "仿真充电状态开始", {
                        "simulated": True, "duration_seconds": step["duration_seconds"]
                    })
                    if cancel.wait(step["duration_seconds"]):
                        raise Canceled()
                    self.store.event(r, "charging_completed", "仿真充电状态完成", {
                        "simulated": True
                    })
                elif action == "enter_idle":
                    self.bridge.stop(rid)
                    r["status"] = "idle"
                    self.store.event(r, "idle", "机器人进入空闲状态", {"observable": True})
                step.update(status="completed", finished=time.time())
                self.store.event(r, "step_completed", f"{step['id']} 完成", {
                    "step_id": step["id"], "action": action
                })
            else:
                r["status"] = "verified"
                r["mission_acceptance"] = {
                    "success": True,
                    "completed_steps": len(r["steps"]),
                    "total_steps": len(r["steps"]),
                    "final_pose_validation": r.get("validation"),
                    "plan_version": r.get("plan_version", 1),
                    "replan_count": len(r.get("plan_revisions", [])),
                }
                self.store.event(r, "mission_validation", "多步骤任务最终验收通过", r["mission_acceptance"])
        except Canceled:
            r["status"] = "canceled"
            if r.get("current_step") is not None:
                step = r["steps"][r["current_step"]]
                step.update(status="canceled", finished=time.time(), failure_reason="用户取消")
            self.store.event(r, "cancel", "用户取消任务")
        except Exception as error:
            r["status"] = "failed"
            r["error"] = str(error)[:500]
            if r.get("current_step") is not None:
                step = r["steps"][r["current_step"]]
                step.update(status="failed", finished=time.time(), failure_reason=r["error"])
            self.store.event(r, "error", "多步骤任务未完成", {"error": r["error"]})
        finally:
            with self.dispatch_lock:
                r["active_target"] = None
                r["finished"] = time.time()
                try:
                    self.store.save(r)
                finally:
                    self.cancel_events.pop(r["id"], None)
                    self.bridge.locks[rid].release()

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
