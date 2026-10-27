import json
import threading
import tomllib
import uuid

import httpx
import pytest

from robot_debug_lab import config
from robot_debug_lab.agents import ModelError, RobotAgent
from robot_debug_lab.openfang import OpenFangModel
from robot_debug_lab.store import Store
from robot_debug_lab.workflow import Workflow


class Bridge:
    def __init__(self):
        self.locks = {r: threading.Lock() for r in config.ROBOTS}
        self.fixed = False
        self.moves = []

    def snapshot(self, rid):
        return {"robot_id": rid, "nodes": [f"/{rid}/controller"]}

    def parameters(self, rid):
        return {"max_speed": 2.0 if self.fixed else 0.0, "command_topic": "turtle1/cmd_vel"}

    def navigate(self, rid, x=8.0, y=8.0, cancel=None):
        self.moves.append(rid)
        return {"success": self.fixed, "error": 0.1 if self.fixed else 3.4}

    def repair(self, rid, cause):
        assert cause == "zero_speed"
        self.fixed = True
        return {"operation": cause}


def remote(handler):
    return OpenFangModel("robot_2", "test-run", httpx.Client(
        base_url="http://127.0.0.1:4200", transport=httpx.MockTransport(handler)))


def test_remote_diagnostic_roundtrip_and_cleanup(tmp_path):
    requests = []
    agent_id = str(uuid.uuid4())
    def handler(request):
        requests.append(request)
        if request.url.path == "/api/agents":
            manifest = tomllib.loads(json.loads(request.content)["manifest_toml"])
            assert manifest["capabilities"]["tools"] == ["rdl_gateway_only"]
            assert manifest["tool_allowlist"] == manifest["tool_blocklist"] == ["rdl_gateway_only"]
            assert manifest["capabilities"]["agent_spawn"] is False
            assert "robot_2" in manifest["model"]["system_prompt"]
            return httpx.Response(201, json={"agent_id": agent_id})
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "killed"})
        body = json.loads(json.loads(request.content)["message"])
        evidence = [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]
        if not evidence:
            call = {"name": "read_parameters", "arguments": {}}
        else:
            initial = json.loads(body["messages"][1]["content"])["initial_evidence"]
            call = {"name": "finish_diagnosis", "arguments": {
                "cause": "zero_speed", "summary": "速度上限为零", "repair_rationale": "恢复限速",
                "evidence_ids": [initial["id"], evidence[0]["id"]]}}
        return httpx.Response(200, json={"response": json.dumps({"calls": [call]}),
                                         "input_tokens": 10, "output_tokens": 5})
    model = remote(handler)
    store = Store(tmp_path / "runs.db")
    run = store.new("robot_2", "ai", "check")
    run["baseline"] = {"success": False}
    try:
        diagnosis = RobotAgent(Bridge(), store, model, robot="robot_2")._diagnose(run, threading.Event(), model)
        assert diagnosis["cause"] == "zero_speed"
        assert run["usage"] == {"prompt_tokens": 20, "completion_tokens": 10}
    finally:
        model.close()
    assert requests[-1].method == "DELETE"
    assert requests[-1].url.path == f"/api/agents/{agent_id}"


@pytest.mark.parametrize("response", ["not json", '{"calls":[]}', '{"calls":[{"name":"shell","arguments":{}}]}'])
def test_remote_rejects_invalid_or_unapproved_calls(response):
    model = remote(lambda _: httpx.Response(200, json={"response": response}))
    model.agent_id = str(uuid.uuid4())
    from robot_debug_lab.agents import TOOLS
    with pytest.raises(ModelError, match="valid gateway"):
        model.complete([], TOOLS)
    model.client.close()


def test_remote_http_error_is_redacted():
    model = remote(lambda _: httpx.Response(401, text="secret credentials"))
    with pytest.raises(ModelError) as error:
        model.complete([], [])
    assert "secret credentials" not in str(error.value)
    model.close()


def test_robot_agents_are_separate_and_scope_bound(tmp_path):
    flow = Workflow(Bridge(), Store(tmp_path / "runs.db"))
    try:
        assert flow.agents["robot_1"] is not flow.agents["robot_2"]
        run = flow.store.new("robot_2", "rules", "check")
        with pytest.raises(ValueError, match="scope"):
            flow.agents["robot_1"].diagnose(run, threading.Event())
    finally:
        flow.close()


def test_rules_workflow_replays_and_releases_robot(tmp_path):
    bridge = Bridge()
    flow = Workflow(bridge, Store(tmp_path / "runs.db"))
    run = flow.submit("robot_2", "rules", "check")
    flow.pool.shutdown(wait=True)
    result = flow.store.get(run["id"])
    assert result["status"] == "verified"
    assert result["baseline"]["success"] is False
    assert result["validation"]["success"] is True
    assert bridge.moves == ["robot_2", "robot_2"]
    assert not bridge.locks["robot_2"].locked()


def test_submission_failure_releases_lock(tmp_path, monkeypatch):
    bridge = Bridge()
    flow = Workflow(bridge, Store(tmp_path / "runs.db"))
    def fail(*args):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(flow.store, "new", fail)
    try:
        with pytest.raises(RuntimeError):
            flow.submit("robot_1", "rules", "check")
        assert not bridge.locks["robot_1"].locked()
    finally:
        flow.close()
