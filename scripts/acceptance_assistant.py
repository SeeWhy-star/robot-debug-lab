"""Live natural-language acceptance; resets simulation and calls the real model.

Run after starting OpenFang and the lab. Results: .runtime/acceptance-assistant.json.
"""
import json
import time
from pathlib import Path

import httpx


def main():
    results = []
    with httpx.Client(base_url="http://127.0.0.1:8765", timeout=90) as client:
        state = client.get("/api/state").json()
        client.headers["X-Lab-Token"] = state["token"]

        def post(path, body=None):
            response = client.post(path, json=body or {})
            response.raise_for_status()
            return response.json()

        def record(name, ok, details):
            results.append({"test": name, "pass": bool(ok), "details": details})
            Path(".runtime/acceptance-assistant.json").write_text(
                json.dumps(results, ensure_ascii=False, indent=2))
            print(name, bool(ok), json.dumps(details, ensure_ascii=False), flush=True)
            assert ok, name

        def await_runs(ids):
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                runs = [client.get("/api/runs/" + rid).json() for rid in ids]
                if all(r["status"] in {"verified", "failed", "unresolved", "diagnosed", "canceled"} for r in runs):
                    return runs
                time.sleep(0.5)
            raise TimeoutError(ids)

        for rid in ["robot_1", "robot_2"]:
            post(f"/api/robots/{rid}/reset")

        for name, prompt in [
            ("ambiguous-request", "让机器人去那里"),
            ("out-of-bounds-request", "让机器人 2 去 (100, 6)"),
        ]:
            response = post("/api/assistant/tasks", {"prompt": prompt})
            record(name, response["run_ids"] == [] and response["plan"]["status"] == "needs_clarification", response)

        response = post("/api/assistant/tasks", {
            "prompt": "让机器人 1 去 (3, 4)，同时让机器人 2 去 (7, 6)。", "auto_repair": False,
        })
        ids = response["run_ids"]
        assert len(ids) == 2, response
        runs = await_runs(ids)
        expected = {"robot_1": [3.0, 4.0], "robot_2": [7.0, 6.0]}
        record("parallel-navigation", all(
            r["status"] == "verified" and r["target"] == expected[r["robot_id"]]
            and r["diagnosis"] is None and r["backend"] == state["runtime"] for r in runs
        ), {"plan": response["plan"], "runs": [
            {"id": r["id"], "robot_id": r["robot_id"], "status": r["status"], "validation": r["validation"]} for r in runs]})

        post("/api/robots/robot_2/fault", {"kind": "topic_mismatch"})
        response = post("/api/assistant/tasks", {
            "prompt": "让机器人 2 去 (6, 9)，失败后允许诊断和限定修复。", "auto_repair": True,
        })
        assert len(response["run_ids"]) == 1, response
        run = await_runs(response["run_ids"])[0]
        record("diagnose-repair-original-target", run["status"] == "verified"
               and run["target"] == [6.0, 9.0] and not run["baseline"]["success"]
               and run["diagnosis"]["cause"] == "topic_mismatch"
               and run["validation"]["target"] == [6.0, 9.0], run)

        post("/api/robots/robot_2/fault", {"kind": "zero_speed"})
        response = post("/api/assistant/tasks", {
            "prompt": "机器人 2 去 (7, 6)，失败时仅诊断，禁止修复。", "auto_repair": True,
        })
        assert len(response["run_ids"]) == 1, response
        run = await_runs(response["run_ids"])[0]
        record("explicit-no-repair", run["status"] == "diagnosed"
               and not any(e["kind"] == "repair" for e in run["events"])
               and response["plan"]["tasks"][0]["auto_repair"] is False,
               {"plan": response["plan"], "run_id": run["id"], "status": run["status"]})
        for rid in ["robot_1", "robot_2"]:
            post(f"/api/robots/{rid}/reset")


if __name__ == "__main__":
    main()
