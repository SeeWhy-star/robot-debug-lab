"""API lifecycle checks against the running local simulation; no model requests."""

import json, time
from pathlib import Path
import httpx

c = httpx.Client(base_url="http://127.0.0.1:8765", timeout=40)
state = c.get("/api/state").json()
token = state["token"]
results = []


def check(name, ok):
    results.append({"test": name, "pass": bool(ok)})
    print(name, ok, flush=True)
    Path(".runtime/acceptance-api.json").write_text(json.dumps(results, indent=2))
    assert ok, name


check(
    "reject-mutation-without-token",
    c.post("/api/robots/robot_1/reset").status_code == 403,
)
c.headers["X-Lab-Token"] = token


def post(path, body={}):
    r = c.post(path, json=body)
    r.raise_for_status()
    return r.json()


check("unknown-robot", c.post("/api/robots/robot_9/reset").status_code == 404)
check(
    "out-of-bounds-goal",
    c.post("/api/robots/robot_1/navigate", json={"x": 100}).status_code == 422,
)
for robot in ["robot_1", "robot_2"]:
    post(f"/api/robots/{robot}/fault", {"kind": "zero_speed"})
ids = post(
    "/api/runs",
    {"robots": ["robot_1", "robot_2"], "mode": "rules", "auto_repair": True},
)["run_ids"]
check(
    "two-concurrent-runs",
    len(ids) == 2 and all(r["busy"] for r in c.get("/api/state").json()["robots"]),
)
check("busy-robot-conflict", c.post("/api/robots/robot_1/reset").status_code == 409)
check("cancel-accepted", post("/api/runs/" + ids[0] + "/cancel")["accepted"])
deadline = time.monotonic() + 40
while time.monotonic() < deadline:
    runs = [c.get("/api/runs/" + i).json() for i in ids]
    if all(
        r["status"] in ["canceled", "verified", "failed", "unresolved"] for r in runs
    ):
        break
    time.sleep(0.3)
check(
    "cancel-isolated-from-peer",
    runs[0]["status"] == "canceled" and runs[1]["status"] == "verified",
)
check(
    "locks-released", not any(r["busy"] for r in c.get("/api/state").json()["robots"])
)
check(
    "export-preserves-evidence",
    c.get("/api/runs/" + ids[1] + "/export").json()["events"] == runs[1]["events"],
)
for robot in ["robot_1", "robot_2"]:
    post(f"/api/robots/{robot}/reset")
