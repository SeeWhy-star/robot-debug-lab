import json
import threading
import tomllib
import uuid

import httpx
import pytest

from robot_debug_lab.agents import ModelError, RobotAgent
from robot_debug_lab.openfang import OpenFangModel
from robot_debug_lab.store import Store


class Bridge:
    robot_ids = ("robot_1",)

    def __init__(self):
        self.locks = {"robot_1": threading.Lock()}

    def topic_health(self, rid):
        return {"raw_scan_hz": 10.0, "nav2_scan_hz": 0.0}

    def snapshot(self, rid):
        return {
            "robot_id": rid,
            "nodes": ["/amcl", "/controller_server"],
            "topics": self.topic_health(rid),
        }

    def parameters(self, rid):
        return {"scan_input": "/scan_faulted", **self.topic_health(rid)}

    def sample(self, rid):
        return {"displacement": 0.0}


def remote(handler):
    return OpenFangModel(
        "robot_1",
        "test-run",
        httpx.Client(
            base_url="http://127.0.0.1:4200",
            transport=httpx.MockTransport(handler),
        ),
    )


def test_remote_diagnostic_roundtrip_and_cleanup(tmp_path):
    requests = []
    agent_id = str(uuid.uuid4())

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/agents":
            manifest = tomllib.loads(json.loads(request.content)["manifest_toml"])
            assert manifest["capabilities"]["tools"] == ["rdl_gateway_only"]
            assert manifest["capabilities"]["agent_spawn"] is False
            assert "robot_1" in manifest["model"]["system_prompt"]
            return httpx.Response(201, json={"agent_id": agent_id})
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "killed"})
        body = json.loads(json.loads(request.content)["message"])
        evidence = [
            json.loads(message["content"])
            for message in body["messages"]
            if message["role"] == "tool"
        ]
        if not evidence:
            call = {"name": "read_parameters", "arguments": {}}
        else:
            initial = json.loads(body["messages"][1]["content"])["initial_evidence"]
            call = {
                "name": "finish_diagnosis",
                "arguments": {
                    "cause": "scan_interrupted",
                    "summary": "原始雷达有数据，Nav2 输入为零。",
                    "repair_rationale": "恢复受控雷达转发并复测。",
                    "evidence_ids": [initial["id"], evidence[0]["id"]],
                },
            }
        return httpx.Response(
            200,
            json={
                "response": json.dumps({"calls": [call]}),
                "input_tokens": 10,
                "output_tokens": 5,
            },
        )

    model = remote(handler)
    store = Store(tmp_path / "runs.db")
    run = store.new("robot_1", "ai", "check")
    run["baseline"] = {"success": False}
    try:
        diagnosis = RobotAgent(
            Bridge(), store, model, robot="robot_1"
        )._diagnose(run, threading.Event(), model)
        assert diagnosis["cause"] == "scan_interrupted"
        assert run["usage"] == {"prompt_tokens": 20, "completion_tokens": 10}
    finally:
        model.close()
    assert requests[-1].method == "DELETE"


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        '{"calls":[]}',
        '{"calls":[{"name":"shell","arguments":{}}]}',
    ],
)
def test_remote_rejects_invalid_or_unapproved_calls(response):
    model = remote(lambda _: httpx.Response(200, json={"response": response}))
    model.agent_id = str(uuid.uuid4())
    from robot_debug_lab.agents import TOOLS

    with pytest.raises(ModelError, match="valid gateway"):
        model.complete([], TOOLS)
    model.client.close()


def test_remote_extracts_strictly_valid_gateway_envelope_from_provider_text():
    response = 'I will inspect first. {"calls":[{"name":"read_parameters","arguments":{}}]}'
    model = remote(lambda _: httpx.Response(200, json={"response": response}))
    model.agent_id = str(uuid.uuid4())
    from robot_debug_lab.agents import TOOLS

    message, _ = model.complete([], TOOLS)
    assert message["tool_calls"][0]["function"]["name"] == "read_parameters"
    model.client.close()


def test_remote_parses_deepseek_dsml_through_same_allowlist():
    response = '''<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="read_parameters">
<｜｜DSML｜｜ parameter name="arguments" string="false">{}</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>'''
    model = remote(lambda _: httpx.Response(200, json={"response": response}))
    model.agent_id = str(uuid.uuid4())
    from robot_debug_lab.agents import TOOLS

    message, _ = model.complete([], TOOLS)
    assert message["tool_calls"][0]["function"]["name"] == "read_parameters"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {}
    model.client.close()


def test_remote_parses_deepseek_dsml_with_one_parameter_per_schema_field():
    response = '''<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="finish_plan">
<｜｜DSML｜｜ parameter name="steps" string="false">[]</｜｜DSML｜｜ parameter>
<｜｜DSML｜｜ parameter name="message" string="true">当前机器人不支持取货。</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>'''
    model = remote(lambda _: httpx.Response(200, json={"response": response}))
    model.agent_id = str(uuid.uuid4())
    from robot_debug_lab.supervisor import SCENE_TOOLS

    message, _ = model.complete([], SCENE_TOOLS)
    call = message["tool_calls"][0]["function"]
    assert call["name"] == "finish_plan"
    assert json.loads(call["arguments"]) == {
        "steps": [], "message": "当前机器人不支持取货。"
    }
    model.client.close()


def test_remote_normalizes_direct_schema_shaped_completion():
    from robot_debug_lab.supervisor import SCENE_TOOLS

    response = json.dumps({
        "steps": [],
        "message": "当前机器人没有取货能力，可以改为到货架旁等待人工装载。",
    }, ensure_ascii=False)
    model = remote(lambda _: httpx.Response(200, json={"response": response}))
    model.agent_id = str(uuid.uuid4())

    message, _ = model.complete([], SCENE_TOOLS)
    call = message["tool_calls"][0]["function"]
    assert call["name"] == "finish_plan"
    assert json.loads(call["arguments"])["steps"] == []
    model.client.close()


def test_remote_normalizes_one_explicit_gateway_call_without_envelope():
    response = json.dumps({"name": "read_parameters", "arguments": {}})
    model = remote(lambda _: httpx.Response(200, json={"response": response}))
    model.agent_id = str(uuid.uuid4())
    from robot_debug_lab.agents import TOOLS

    message, _ = model.complete([], TOOLS)
    assert message["tool_calls"][0]["function"]["name"] == "read_parameters"
    model.client.close()


def test_remote_http_error_is_redacted():
    model = remote(lambda _: httpx.Response(401, text="secret credentials"))
    with pytest.raises(ModelError) as error:
        model.complete([], [])
    assert "secret credentials" not in str(error.value)
    model.close()
