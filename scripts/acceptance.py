"""Independent black-box acceptance. Known injected labels stay in this client."""

import json, time, sys
from pathlib import Path
import httpx

c = httpx.Client(base_url="http://127.0.0.1:8765", timeout=40)
token = c.get("/api/state").json()["token"]
c.headers["X-Lab-Token"] = token
results = []


def post(path, body=None):
    r = c.post(path, json=body or {})
    r.raise_for_status()
    return r.json()


def await_run(rid):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        r = c.get("/api/runs/" + rid).json()
        if r["status"] in [
            "verified",
            "unresolved",
            "failed",
            "canceled",
            "diagnosed",
            "interrupted",
        ]:
            return r
        time.sleep(1)
    raise TimeoutError(rid)


def record(name, ok, details):
    results.append({"test": name, "pass": bool(ok), "details": details})
    Path(".runtime/acceptance.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2)
    )
    print(name, bool(ok), json.dumps(details, ensure_ascii=False), flush=True)


post("/api/robots/robot_1/reset")
r = post("/api/robots/robot_1/navigate")
record("healthy-navigation", r["success"], r)
for fault in ["topic_mismatch", "zero_speed", "controller_stopped"]:
    post("/api/robots/robot_2/fault", {"kind": fault})
    rid = post(
        "/api/runs",
        {
            "robots": ["robot_2"],
            "mode": "ai",
            "prompt": "机器人无法到达目标，请调查实际运行状态，确认原因并验证修复。",
            "auto_repair": True,
        },
    )["run_ids"][0]
    r = await_run(rid)
    record(
        fault,
        r["status"] == "verified"
        and r["diagnosis"]["cause"] == fault
        and not r["baseline"]["success"],
        {
            "run_id": rid,
            "status": r["status"],
            "diagnosis": r.get("diagnosis"),
            "baseline": r.get("baseline"),
            "validation": r.get("validation"),
            "usage": r["usage"],
            "error": r.get("error"),
        },
    )
    if r["status"] == "failed":
        break
Path(".runtime/acceptance.json").write_text(
    json.dumps(results, ensure_ascii=False, indent=2)
)
sys.exit(not all(x["pass"] for x in results))
