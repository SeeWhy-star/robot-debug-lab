import asyncio, json, os, secrets, time
from contextlib import asynccontextmanager, contextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal
from .config import (
    ROOT,
    MODEL,
    API_KEY,
    AGENT_BACKEND,
    OPENFANG_MODEL,
    SIM_BACKEND,
)
from .store import Store
from .workflow import Workflow
from .supervisor import Supervisor
from .mission_replanner import MissionReplanner
from .agents import ModelError
from .fleet import FleetSupervisor, explicit_coordinates
from .contracts import CONTRACT_VERSION, ACTIVE_TARGET_PHASES, public_run
from .metrics import build_metrics


@asynccontextmanager
async def lifespan(app):
    from .nav2_bridge import Nav2Bridge

    bridge = await asyncio.to_thread(Nav2Bridge)
    app.state.bridge = bridge
    app.state.store = Store()
    app.state.supervisor = Supervisor(
        app.state.store, robot_state=lambda rid: bridge.snapshot(rid)
    )
    app.state.replanner = MissionReplanner(
        app.state.store, bridge, app.state.supervisor.scene
    )
    app.state.workflow = Workflow(
        bridge, app.state.store, replanner=app.state.replanner
    )
    app.state.fleet_supervisor = FleetSupervisor(
        app.state.store, bridge, app.state.supervisor
    )
    app.state.token = secrets.token_urlsafe(32)
    yield
    await asyncio.to_thread(app.state.workflow.close)
    await asyncio.to_thread(bridge.close)


app = FastAPI(
    title="Robot Debug Lab",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


class Diagnose(BaseModel):
    robots: list[str] = Field(
        default=["robot_1"], min_length=1, max_length=2
    )
    mode: Literal["ai", "rules"] = "ai"
    prompt: str = Field(
        default="排查机器人无法到达目标的原因，用证据验证并给出修复方案。",
        min_length=1,
        max_length=2000,
    )
    auto_repair: bool = True


class Fault(BaseModel):
    kind: Literal[
        "scan_interrupted", "navigator_inactive", "controller_velocity_zero",
        "localization_drift",
    ]


class FaultSchedule(Fault):
    navigation_attempt: int = Field(ge=1, le=8)


class Scenario(BaseModel):
    kind: Literal["shelf_a_primary_blocked"]


class AssistantRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    auto_repair: bool = True
    parent_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{12}$")
    recovery_mode: Literal["ai", "rules"] = "ai"
    execute: bool = True
    robot_id: Literal["robot_1", "robot_2"] = "robot_1"


class StoredPlanRun(BaseModel):
    recovery_mode: Literal["ai", "rules"] = "ai"


class FleetRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    auto_repair: bool = True
    recovery_mode: Literal["ai", "rules"] = "ai"
    execute: bool = True
    allow_handoff: bool = True
    traffic_wait_timeout_seconds: float = Field(
        default=45.0, ge=1.0, le=120.0, allow_inf_nan=False
    )


class Target(BaseModel):
    x: float = Field(default=2.0, ge=-4.0, le=4.0, allow_inf_nan=False)
    y: float = Field(default=0.5, ge=-4.0, le=4.0, allow_inf_nan=False)


@app.middleware("http")
async def local_guard(request: Request, call_next):
    host = request.headers.get("host", "").split(":")[0]
    if host not in ["127.0.0.1", "localhost", "testserver"]:
        response = JSONResponse({"detail": "Local access only"}, status_code=403)
    elif request.method in ["POST", "PUT", "DELETE", "PATCH"]:
        if not secrets.compare_digest(
            request.headers.get("x-lab-token", ""),
            getattr(app.state, "token", "invalid"),
        ):
            response = JSONResponse(
                {"detail": "Refresh the local dashboard to authorize this action"},
                status_code=403,
            )
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@contextmanager
def exclusive(rid):
    if rid not in app.state.bridge.locks:
        raise HTTPException(404, "Unknown robot")
    lock = app.state.bridge.locks[rid]
    if not lock.acquire(False):
        raise HTTPException(409, "Robot has an active task; cancel it first")
    try:
        yield
    finally:
        lock.release()


@contextmanager
def exclusive_fleet():
    """Reserve every robot before mutating the shared Gazebo world."""
    acquired = []
    try:
        for rid in sorted(app.state.bridge.robot_ids):
            lock = app.state.bridge.locks[rid]
            if not lock.acquire(False):
                raise HTTPException(
                    409, f"{rid} has an active task; shared world was not changed"
                )
            acquired.append(lock)
        yield
    finally:
        for lock in reversed(acquired):
            lock.release()


@app.get("/api/state")
def state():
    runs = [public_run(run) for run in app.state.store.all()]
    active_targets = {
        run["robot_id"]: run["active_target"]
        for run in runs
        if run.get("active_target") is not None
        and run["phase"] in ACTIVE_TARGET_PHASES
    }
    return {
        "contract_version": CONTRACT_VERSION,
        "server_time": time.time(),
        "robots": app.state.bridge.public(),
        "runs": runs,
        "active_run_ids": [run["id"] for run in runs if not run["terminal"]],
        "active_targets": active_targets,
        "traffic": app.state.workflow.traffic.snapshot(),
        "plans": app.state.store.recent_plans(),
        "model": OPENFANG_MODEL if AGENT_BACKEND == "openfang" else MODEL,
        "ai_configured": AGENT_BACKEND == "openfang" or bool(API_KEY),
        "token": app.state.token,
        "ros_domain": os.environ.get("ROS_DOMAIN_ID"),
        "runtime": AGENT_BACKEND,
        "simulation": SIM_BACKEND,
        "agents": [
            {"robot_id": rid, "busy": app.state.bridge.locks[rid].locked()}
            for rid in app.state.bridge.robot_ids
        ],
    }


@app.get("/api/metrics")
def metrics(request: Request, limit: int = 100, experiment_id: str | None = None):
    strategies = request.query_params.getlist("strategy")
    allowed = {"rules", "no_tools_llm", "tools_llm"}
    if not 1 <= limit <= 100:
        raise HTTPException(422, "limit must be between 1 and 100")
    if any(strategy not in allowed for strategy in strategies):
        raise HTTPException(422, "Unknown evaluation strategy")
    return build_metrics(
        app.state.store,
        experiment_id=experiment_id,
        strategies=strategies,
        limit=limit,
    )


@app.get("/api/scene/objects")
def scene_objects():
    return app.state.supervisor.scene.list_scene_objects()


@app.get("/api/scene/objects/{object_id}")
def scene_object(object_id: str):
    try:
        return {
            **app.state.supervisor.scene.get_object_pose(object_id),
            "safe_approach": app.state.supervisor.scene.get_safe_approach_pose(object_id)["pose"],
        }
    except ValueError as error:
        raise HTTPException(404, str(error)) from None


@app.get("/api/events")
async def events():
    async def stream():
        while True:
            yield "data: " + json.dumps(state(), ensure_ascii=False) + "\n\n"
            await asyncio.sleep(0.6)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


@app.get("/api/source-cases/nav2-5710")
def source_case():
    from .source_case import public_summary

    return public_summary()


@app.post("/api/robots/{rid}/fault")
def inject(rid: str, f: Fault):
    with exclusive(rid):
        return app.state.bridge.inject(rid, f.kind)


@app.post("/api/robots/{rid}/fault-schedule")
def schedule_fault(rid: str, value: FaultSchedule):
    with exclusive(rid):
        return app.state.bridge.schedule_fault(
            rid, value.kind, value.navigation_attempt
        )


@app.post("/api/robots/{rid}/reset")
def reset(rid: str):
    if rid not in app.state.bridge.locks:
        raise HTTPException(404, "Unknown robot")
    with exclusive_fleet():
        result = app.state.bridge.reset(rid)
        app.state.supervisor.scene.clear_runtime_poses()
        return result


@app.post("/api/robots/{rid}/scenario")
def scenario(rid: str, value: Scenario):
    if rid not in app.state.bridge.locks:
        raise HTTPException(404, "Unknown robot")
    with exclusive_fleet():
        result = app.state.bridge.set_scenario(rid, value.kind)
        app.state.supervisor.scene.set_runtime_pose(
            "temporary_crate",
            {"x": 2.0, "y": 0.5, "z": 0.35, "yaw": 0.0},
            {"x": 1.1, "y": 0.5, "yaw": 0.0},
        )
        return result


@app.post("/api/robots/{rid}/navigate")
def navigate(rid: str, t: Target):
    with exclusive(rid):
        return app.state.bridge.navigate(rid, t.x, t.y)


@app.post("/api/runs")
def diagnose(q: Diagnose):
    if q.mode == "ai" and AGENT_BACKEND == "direct" and not API_KEY:
        raise HTTPException(409, "Configure model credentials first")
    ids = list(dict.fromkeys(q.robots))
    if any(rid not in app.state.bridge.locks for rid in ids):
        raise HTTPException(404, "Unknown robot in current simulation")
    if any(app.state.bridge.locks[r].locked() for r in ids):
        raise HTTPException(409, "Selected robot busy")
    try:
        target = (2.0, 0.5)
        runs = app.state.workflow.submit_many([
            {"robot_id": rid, "x": target[0], "y": target[1], "auto_repair": q.auto_repair}
            for rid in ids
        ], q.mode, q.prompt)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"run_ids": [r["id"] for r in runs]}


@app.post("/api/assistant/tasks")
def assistant_task(q: AssistantRequest):
    try:
        plan = app.state.supervisor.plan(
            q.prompt, q.auto_repair, q.parent_id, q.robot_id
        )
    except ModelError as e:
        raise HTTPException(502, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    if plan["status"] == "needs_clarification":
        return {"plan": plan, "run_ids": []}
    if not q.execute:
        return {"plan": plan, "run_ids": []}
    return _dispatch_plan(plan, q.recovery_mode)


@app.post("/api/fleet/tasks")
def fleet_task(q: FleetRequest):
    if AGENT_BACKEND == "direct" and not API_KEY:
        raise HTTPException(409, "Configure model credentials first")
    try:
        plan = app.state.fleet_supervisor.plan(
            q.prompt,
            q.auto_repair,
            q.allow_handoff,
            q.traffic_wait_timeout_seconds,
        )
        if plan["status"] == "not_executable":
            return {"plan": plan, "run_ids": []}
        if not q.execute:
            return {"plan": plan, "run_ids": []}
        return _dispatch_fleet_plan(plan, q.recovery_mode)
    except ModelError as error:
        raise HTTPException(502, str(error)) from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


def _validate_stored_fleet_plan(plan):
    """Revalidate every stored assignment against current trusted scene data."""
    if (
        plan.get("plan_type") != "fleet"
        or plan.get("status") not in {"ready", "dispatched"}
        or not plan.get("assignments")
    ):
        raise ValueError("只能执行后端已验证且包含分工的车队计划")
    assignments = plan["assignments"]
    robots = [item.get("robot_id") for item in assignments]
    if len(assignments) > 2 or len(set(robots)) != len(robots):
        raise ValueError("存储车队计划包含无效机器人分工，请重新规划")
    scene = app.state.supervisor.scene
    coordinates = explicit_coordinates(plan.get("request_text", ""))
    from .config import ROBOT_STARTS, ROBOTS
    from .spatial import map_fingerprint, final_navigation_targets

    spatial_targets = {}
    spatial_analysis = plan.get("spatial_analysis")
    if spatial_analysis is not None:
        try:
            if not spatial_analysis.get("resolved"):
                raise ValueError
            evidence = spatial_analysis["evidence"]
            if evidence["map_fingerprint"] != map_fingerprint():
                raise ValueError("占据地图已变化，请重新执行空间分析")
            spatial_targets = {
                item["robot_id"]: (float(item["x"]), float(item["y"]))
                for item in spatial_analysis["assignments"]
            }
            if set(spatial_targets) != set(ROBOTS):
                raise ValueError
        except ValueError as error:
            if str(error):
                raise
            raise ValueError("存储车队计划包含无效空间分析证据") from None
        except (KeyError, TypeError):
            raise ValueError("存储车队计划包含无效空间分析证据") from None

    try:
        traffic_timeout = float(plan.get("traffic_wait_timeout_seconds", 45.0))
    except (TypeError, ValueError):
        raise ValueError("存储车队计划包含无效路线等待上限") from None
    if not 1.0 <= traffic_timeout <= 120.0:
        raise ValueError("存储车队计划包含无效路线等待上限")

    for assignment in assignments:
        robot = assignment.get("robot_id")
        if robot not in ROBOTS or not assignment.get("steps"):
            raise ValueError("存储车队计划包含无效机器人分工，请重新规划")
        for step in assignment["steps"]:
            if step.get("action") not in {
                "navigate", "wait", "return_to_start", "enter_idle", "simulate_charge"
            }:
                raise ValueError("存储车队计划包含不支持的步骤，请重新规划")
            if step.get("action") not in {"navigate", "return_to_start"}:
                continue
            destination = step.get("destination_id")
            if destination == "start" or step.get("action") == "return_to_start":
                x, y, _ = ROBOT_STARTS[robot]
                expected = {"x": x, "y": y}
            elif destination:
                approach_id = step.get("approach_id")
                if not approach_id:
                    raise ValueError("存储车队计划缺少语义接近点，请重新规划")
                try:
                    expected = scene.get_approach_pose(destination, approach_id)["pose"]
                except ValueError:
                    raise ValueError("语义场景已变化，请重新规划") from None
            else:
                try:
                    point = (float(step["x"]), float(step["y"]))
                except (KeyError, TypeError, ValueError):
                    raise ValueError("存储车队计划包含无效坐标，请重新规划") from None
                if point not in coordinates and spatial_targets.get(robot) != point:
                    raise ValueError(
                        "存储车队计划包含未经用户明确提供或空间分析工具授权的坐标"
                    )
                expected = {"x": point[0], "y": point[1]}
            try:
                changed = any(
                    abs(float(step[axis]) - float(expected[axis])) > 1e-6
                    for axis in ("x", "y")
                )
            except (KeyError, TypeError, ValueError):
                raise ValueError("存储车队计划包含无效坐标，请重新规划") from None
            if changed:
                raise ValueError("车队语义场景坐标已变化，请重新规划")
    if spatial_targets:
        planned_targets = final_navigation_targets(assignments)
        if planned_targets != spatial_targets:
            raise ValueError("空间任务没有完整保留工具生成的双机器人站位")
        if plan.get("allow_handoff"):
            raise ValueError("双机器人空间目标不允许单机接替")


def _dispatch_fleet_plan(plan, recovery_mode):
    try:
        _validate_stored_fleet_plan(plan)
        runs = app.state.workflow.submit_fleet(plan, recovery_mode)
    except Exception as error:
        if isinstance(error, ValueError):
            raise HTTPException(409, str(error))
        raise
    current_ids = list(plan.get("run_ids") or [])
    new_ids = [run["id"] for run in runs]
    plan.update(status="dispatched", run_ids=current_ids + new_ids)
    app.state.store.save_plan(plan)
    return {"plan": plan, "run_ids": new_ids}


def _validate_stored_plan(plan):
    if plan.get("status") not in {"ready", "dispatched"} or not plan.get("steps"):
        raise ValueError("只能执行后端已验证且包含步骤的计划")
    scene = app.state.supervisor.scene
    robot = plan.get("robot_id", "robot_1")
    for step in plan["steps"]:
        destination = step.get("destination_id")
        if not destination:
            continue
        approach_id = step.get("approach_id")
        if not approach_id:
            raise ValueError("存储计划缺少语义接近点，请重新规划")
        if destination == "start":
            from .config import ROBOT_STARTS

            x, y, _ = ROBOT_STARTS[robot]
            current = {"x": x, "y": y}
        else:
            try:
                current = scene.get_approach_pose(destination, approach_id)["pose"]
            except ValueError:
                raise ValueError("语义场景已变化，请重新规划") from None
        if any(
            abs(float(step[axis]) - float(current[axis])) > 1e-6
            for axis in ("x", "y")
        ):
            raise ValueError("语义场景坐标已变化，请重新规划")


def _dispatch_plan(plan, recovery_mode):
    try:
        _validate_stored_plan(plan)
        run = app.state.workflow.submit_mission(plan, recovery_mode)
    except Exception as e:
        if isinstance(e, ValueError):
            raise HTTPException(409, str(e))
        raise
    run_ids = list(plan.get("run_ids") or [])
    run_ids.append(run["id"])
    plan.update(status="dispatched", run_ids=run_ids)
    app.state.store.save_plan(plan)
    return {"plan": plan, "run_ids": [run["id"]]}


@app.get("/api/assistant/plans/{pid}")
def get_plan(pid: str):
    plan = app.state.store.get_plan(pid)
    if plan is None:
        raise HTTPException(404, "Plan not found")
    return plan


@app.post("/api/assistant/plans/{pid}/runs")
def run_stored_plan(pid: str, request: StoredPlanRun):
    plan = app.state.store.get_plan(pid)
    if plan is None:
        raise HTTPException(404, "Plan not found")
    return _dispatch_plan(plan, request.recovery_mode)


@app.post("/api/fleet/plans/{pid}/runs")
def run_stored_fleet_plan(pid: str, request: StoredPlanRun):
    plan = app.state.store.get_plan(pid)
    if plan is None:
        raise HTTPException(404, "Plan not found")
    return _dispatch_fleet_plan(plan, request.recovery_mode)


@app.post("/api/runs/{rid}/cancel")
def cancel(rid: str):
    return {"accepted": app.state.workflow.cancel(rid)}


@app.get("/api/runs/{rid}")
def get_run(rid: str):
    r = app.state.store.get(rid)
    if not r:
        raise HTTPException(404, "Run not found")
    return public_run(r)


@app.get("/api/runs/{rid}/export")
def export(rid: str):
    r = get_run(rid)
    return JSONResponse(
        r, headers={"Content-Disposition": f'attachment; filename="run-{rid}.json"'}
    )


@app.get("/")
def home():
    return FileResponse(ROOT / "web/dist/index.html")


app.mount(
    "/assets",
    StaticFiles(directory=ROOT / "web/dist/assets", check_dir=False),
    name="assets",
)
