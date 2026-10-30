import asyncio, json, os, secrets
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


@asynccontextmanager
async def lifespan(app):
    from .nav2_bridge import Nav2Bridge

    bridge = await asyncio.to_thread(Nav2Bridge)
    app.state.bridge = bridge
    app.state.store = Store()
    app.state.supervisor = Supervisor(
        app.state.store, robot_state=lambda: bridge.snapshot("robot_1")
    )
    app.state.replanner = MissionReplanner(
        app.state.store, bridge, app.state.supervisor.scene
    )
    app.state.workflow = Workflow(
        bridge, app.state.store, replanner=app.state.replanner
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
        default=["robot_1"], min_length=1, max_length=1
    )
    mode: Literal["ai", "rules"] = "ai"
    prompt: str = Field(
        default="排查机器人无法到达目标的原因，用证据验证并给出修复方案。",
        min_length=1,
        max_length=2000,
    )
    auto_repair: bool = True


class Fault(BaseModel):
    kind: Literal["scan_interrupted", "navigator_inactive", "controller_velocity_zero"]


class Scenario(BaseModel):
    kind: Literal["shelf_a_primary_blocked"]


class AssistantRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    auto_repair: bool = True
    parent_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{12}$")


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


@app.get("/api/state")
def state():
    runs = app.state.store.all()
    active_targets = {
        run["robot_id"]: run["active_target"]
        for run in runs
        if run.get("active_target") is not None
        and run.get("status") not in {"verified", "unresolved", "failed", "canceled", "diagnosed"}
    }
    return {
        "robots": app.state.bridge.public(),
        "runs": runs,
        "active_targets": active_targets,
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


@app.post("/api/robots/{rid}/reset")
def reset(rid: str):
    with exclusive(rid):
        result = app.state.bridge.reset(rid)
        app.state.supervisor.scene.clear_runtime_poses()
        return result


@app.post("/api/robots/{rid}/scenario")
def scenario(rid: str, value: Scenario):
    with exclusive(rid):
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
        plan = app.state.supervisor.plan(q.prompt, q.auto_repair, q.parent_id)
    except ModelError as e:
        raise HTTPException(502, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    if plan["status"] == "needs_clarification":
        return {"plan": plan, "run_ids": []}
    try:
        run = app.state.workflow.submit_mission(plan, "ai")
    except Exception as e:
        plan["status"] = "rejected"
        app.state.store.save_plan(plan)
        if isinstance(e, ValueError):
            raise HTTPException(409, str(e))
        raise
    plan.update(status="dispatched", run_ids=[run["id"]])
    app.state.store.save_plan(plan)
    return {"plan": plan, "run_ids": plan["run_ids"]}


@app.get("/api/assistant/plans/{pid}")
def get_plan(pid: str):
    plan = app.state.store.get_plan(pid)
    if plan is None:
        raise HTTPException(404, "Plan not found")
    return plan


@app.post("/api/runs/{rid}/cancel")
def cancel(rid: str):
    return {"accepted": app.state.workflow.cancel(rid)}


@app.get("/api/runs/{rid}")
def get_run(rid: str):
    r = app.state.store.get(rid)
    if not r:
        raise HTTPException(404, "Run not found")
    return r


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
