"""Per-robot tool loops with separate contexts; no fault-injector access."""

import json, time
import httpx
from . import config

CAUSES = [
    "scan_interrupted",
    "navigator_inactive",
    "controller_velocity_zero",
    "healthy",
    "unknown",
]


class ModelError(RuntimeError):
    pass


class Canceled(RuntimeError):
    pass


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": n,
            "description": d,
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    }
    for n, d in [
        (
            "inspect_graph",
            "Read the live Nav2 ROS node graph, sensor rates and robot pose.",
        ),
        (
            "read_parameters",
            "Read guarded laser input, Nav2 lifecycle state, and controller velocity limits.",
        ),
        (
            "sample_motion",
            "Sample observed pose twice and measure displacement. A stationary robot alone is not proof of fault.",
        ),
        (
            "measure_topics",
            "Measure raw laser data and the laser stream delivered to Nav2. Use this to distinguish a dead sensor from an interrupted data path.",
        ),
    ]
]
TOOLS.append(
    {
        "type": "function",
        "function": {
            "name": "finish_diagnosis",
            "description": "Finish with a concise evidence-based diagnosis; use unknown if evidence is insufficient.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cause": {"type": "string", "enum": CAUSES},
                    "summary": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "repair_rationale": {"type": "string"},
                },
                "required": ["cause", "summary", "evidence_ids", "repair_rationale"],
                "additionalProperties": False,
            },
        },
    }
)


class CompatibleModel:
    def complete(self, messages, tools):
        if not config.API_KEY:
            raise ModelError(
                "No model key configured; choose explicit rules mode or configure .env"
            )
        payload = {
            "model": config.MODEL,
            "messages": messages,
            "tools": tools,
            "max_tokens": 1800,
            "stream": False,
        }
        if "api.deepseek.com" in config.BASE_URL:
            payload["thinking"] = {"type": "disabled"}
        try:
            with httpx.Client(timeout=45) as c:
                r = c.post(
                    config.BASE_URL + "/chat/completions",
                    headers={"Authorization": "Bearer " + config.API_KEY},
                    json=payload,
                )
            if r.status_code != 200:
                raise ModelError(
                    f"Model API HTTP {r.status_code}; no fallback diagnosis was fabricated"
                )
            d = r.json()
            return d["choices"][0]["message"], d.get("usage", {})
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise ModelError(
                "Model response unavailable or invalid: " + type(e).__name__
            ) from None


class RobotAgent:
    def __init__(self, bridge, store, model=None, robot=None):
        self.bridge = bridge
        self.store = store
        self.model = model or CompatibleModel()
        self.model_override = model is not None
        self.robot = config.robot_id(robot) if robot else None

    def tool(self, name, r):
        rid = r["robot_id"]
        if self.robot is not None and rid != self.robot:
            raise ValueError("Agent robot scope mismatch")
        if name == "inspect_graph":
            data = self.bridge.snapshot(rid)
        elif name == "read_parameters":
            data = self.bridge.parameters(rid)
        elif name == "sample_motion":
            data = self.bridge.sample(rid)
        elif name == "measure_topics":
            data = self.bridge.topic_health(rid)
        else:
            raise ValueError("Unknown read-only tool")
        return self.store.event(r, "tool", name, data)

    def diagnose(self, r, cancel):
        if self.robot is not None and r["robot_id"] != self.robot:
            raise ValueError("Agent robot scope mismatch")
        if r["mode"] != "rules" and config.AGENT_BACKEND == "openfang" and not self.model_override:
            from .openfang import OpenFangModel
            remote = OpenFangModel(r["robot_id"], r["id"])
            try:
                return self._diagnose(r, cancel, remote)
            finally:
                try:
                    remote.close()
                except ModelError:
                    self.store.event(r, "warning", "OpenFang 会话清理失败", {"agent_id": remote.agent_id})
        return self._diagnose(r, cancel, self.model)

    def _diagnose(self, r, cancel, model):
        evidence = {}

        def collect(name):
            e = self.tool(name, r)
            evidence[e["id"]] = e
            return e

        # Initial evidence is captured deterministically, further tools selected by model.
        initial = collect("inspect_graph")
        if r["mode"] == "rules":
            p = collect("read_parameters")
            cause = infer(initial["data"], p["data"])
            return dict(
                cause=cause,
                summary="规则基线：" + cause,
                evidence_ids=[initial["id"], p["id"]],
                repair_rationale="根据节点、连接关系与参数匹配固定规则。",
            )
        system = f"""You are the dedicated diagnostic agent for {r["robot_id"]} in a real Nav2 + Gazebo simulation. Investigate failed navigation using only observed evidence. Distinguish mechanisms: a live raw /scan with zero-rate /scan_faulted supports scan_interrupted; bt_navigator not active supports navigator_inactive; controller FollowPath vx_max=0 and vx_min=0 supports controller_velocity_zero. Use inspect_graph, measure_topics, read_parameters and sample_motion as needed. Never infer a hidden injected label. Cite only actual E identifiers. Different causes require their matching evidence and repair rationale. Summaries and rationale must be Chinese. Finish with finish_diagnosis; use unknown if evidence is insufficient. At most 6 rounds."""
        messages = [
            {
                "role": "system",
                "content": system,
            }
        ]
        messages.append(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "request": r["prompt"],
                        "baseline": r["baseline"],
                        "initial_evidence": initial,
                    },
                    ensure_ascii=False,
                ),
            }
        )
        protocol_retries = 0
        for step in range(6):
            if cancel.is_set():
                raise Canceled()
            self.store.event(
                r,
                "model",
                "模型选择下一步调查",
                {"round": step + 1, "model": config.OPENFANG_MODEL if config.AGENT_BACKEND == "openfang" else config.MODEL,
                 "backend": config.AGENT_BACKEND},
            )
            try:
                message, usage = model.complete(messages, TOOLS)
            except ModelError as error:
                if (
                    "valid gateway tool requests" not in str(error)
                    or protocol_retries >= 1
                ):
                    raise
                protocol_retries += 1
                self.store.event(
                    r,
                    "warning",
                    "模型工具格式无效，要求按协议重试",
                    {"attempt": protocol_retries, "fallback": False},
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response did not match the gateway tool protocol. "
                            "Reply only with valid calls to the provided tools; do not use free text."
                        ),
                    }
                )
                continue
            for k in ["prompt_tokens", "completion_tokens"]:
                r["usage"][k] += int(usage.get(k, 0))
            self.store.save(r)
            # Preserve protocol fields (including provider reasoning if required) in memory only.
            messages.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                messages.append(
                    {
                        "role": "user",
                        "content": "Use the available tools and finish_diagnosis; do not conclude in free text.",
                    }
                )
                continue
            if len(calls) > 5:
                raise ModelError("Model exceeded per-round tool limit")
            for call in calls:
                if cancel.is_set():
                    raise Canceled()
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments", "{}"))
                    if name == "finish_diagnosis":
                        required = {
                            "cause",
                            "summary",
                            "evidence_ids",
                            "repair_rationale",
                        }
                        if (
                            not isinstance(args, dict)
                            or set(args) != required
                            or args["cause"] not in CAUSES
                        ):
                            raise ValueError("Invalid diagnosis schema")
                        if (
                            not isinstance(args["evidence_ids"], list)
                            or not args["evidence_ids"]
                            or any(x not in evidence for x in args["evidence_ids"])
                        ):
                            raise ValueError(
                                "Citations must refer to observed evidence"
                            )
                        if not all(
                            isinstance(args[k], str) and len(args[k]) <= 4000
                            for k in ["summary", "repair_rationale"]
                        ):
                            raise ValueError("Invalid explanation")
                        if not any(
                            e["title"] == "read_parameters" for e in evidence.values()
                        ):
                            raise ValueError("Read parameters before concluding")
                        parameter_events = [
                            e for e in evidence.values() if e["title"] == "read_parameters"
                        ]
                        inferred = infer(initial["data"], parameter_events[-1]["data"])
                        if args["cause"] != inferred:
                            raise ValueError("Diagnosis is not supported by the observed mechanism")
                        if not any(e["id"] in args["evidence_ids"] for e in parameter_events):
                            raise ValueError("Cite the mechanism evidence used for diagnosis")
                        return args
                    if args != {}:
                        raise ValueError(
                            "Observation tools accept no arguments; robot scope is server-bound"
                        )
                    result = collect(name)
                except (ValueError, TypeError, KeyError) as e:
                    result = {"error": str(e)}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        raise ModelError(
            "Model investigation reached the 6-round limit without a valid diagnosis"
        )


def infer(graph, params):
    lifecycle = params.get("nav2_lifecycle", graph.get("nav2_lifecycle", {}))
    navigator = lifecycle.get("bt_navigator", {})
    if navigator.get("label") not in {None, "active"}:
        return "navigator_inactive"
    controller = params.get("controller", {})
    if controller.get("vx_max") == 0.0 and controller.get("vx_min") == 0.0:
        return "controller_velocity_zero"
    topics = graph.get("topics", {})
    if topics.get("raw_scan_hz", 0) > 0 and topics.get("nav2_scan_hz", 0) == 0:
        return "scan_interrupted"
    if topics.get("raw_scan_hz", 0) > 0 and topics.get("nav2_scan_hz", 0) > 0:
        return "healthy"
    return "unknown"
