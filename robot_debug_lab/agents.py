"""Per-robot tool loops with separate contexts; no fault-injector access."""

import hashlib, json, time
import httpx
from . import config
from .contracts import (
    DIAGNOSTIC_PROMPT_VERSION,
    KNOWN_REPAIRS,
    SUBSYSTEMS,
    no_tools_input,
    validate_diagnosis,
    validate_diagnosis_evidence,
)

PROMPT_VERSION = DIAGNOSTIC_PROMPT_VERSION

CAUSES = [
    "scan_interrupted",
    "navigator_inactive",
    "controller_velocity_zero",
    "localization_drift",
    "healthy",
    "unknown",
]

DIAGNOSTIC_SYSTEM_PROMPT = """You are the dedicated diagnostic agent for {robot_id} in a Nav2 and Gazebo robot system. The observable system contains a namespaced ROS 2 graph, sensor data paths, Nav2 lifecycle nodes, planner and controller configuration, costmaps, localization, coordinate-transform dependencies, and independent Gazebo physical truth.

Use only the supplied read-only investigation tools. Normal operation has the required nodes active, related upstream and downstream observations mutually consistent, guarded parameters inside their approved operating ranges, localization aligned with independent physical truth, required navigation dependencies available and fresh, and a completed task accepted by both Nav2 and Gazebo verification. Treat these as invariants to investigate, not as a list of possible faults. Do not assume the known-cause enum is exhaustive.

Base every conclusion on observed evidence from this run and cite only its E identifiers. If evidence supports an enumerated known cause, request only its schema-defined repair. If the evidence establishes a problem outside that enum, classify it as unknown_fault, identify the suspected subsystem, request no repair, and recommend human investigation. If observations cannot distinguish a fault, use insufficient_evidence. Never infer an injected label or invent tool output. Summaries, ruled-out reasons and recommendations must be concise Chinese. Finish through finish_diagnosis within 6 rounds."""


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
            "Read the namespaced ROS node graph and current navigation observations.",
        ),
        (
            "read_parameters",
            "Read guarded navigation lifecycle and controller configuration.",
        ),
        (
            "sample_motion",
            "Sample physical pose twice and report measured displacement.",
        ),
        (
            "measure_topics",
            "Measure related upstream and downstream sensor stream rates.",
        ),
        (
            "inspect_transforms",
            "Read availability and freshness of the coordinate transforms required by this robot's navigation stack.",
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
                    "classification": {
                        "type": "string",
                        "enum": [
                            "known_fault", "unknown_fault",
                            "insufficient_evidence", "healthy",
                        ],
                    },
                    "known_cause": {
                        "type": ["string", "null"],
                        "enum": [*KNOWN_REPAIRS, None],
                    },
                    "suspected_subsystem": {
                        "type": "string", "enum": sorted(SUBSYSTEMS),
                    },
                    "summary": {"type": "string"},
                    "cited_evidence": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "ruled_out": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "hypothesis": {"type": "string"},
                                "reason": {"type": "string"},
                                "evidence_ids": {
                                    "type": "array", "items": {"type": "string"},
                                },
                            },
                            "required": ["hypothesis", "reason", "evidence_ids"],
                            "additionalProperties": False,
                        },
                    },
                    "repair_requested": {"type": "boolean"},
                    "requested_repair": {
                        "type": ["string", "null"],
                        "enum": [*KNOWN_REPAIRS.values(), None],
                    },
                    "recommended_action": {"type": "string"},
                },
                "required": [
                    "classification", "known_cause", "suspected_subsystem",
                    "summary", "cited_evidence", "ruled_out",
                    "repair_requested", "requested_repair", "recommended_action",
                ],
                "additionalProperties": False,
            },
        },
    }
)

DIAGNOSTIC_PROMPT_FINGERPRINT = hashlib.sha256(
    json.dumps(
        {"system_prompt_template": DIAGNOSTIC_SYSTEM_PROMPT, "tools": TOOLS},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()


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
        started = time.monotonic()
        if name == "inspect_graph":
            data = self.bridge.snapshot(rid)
        elif name == "read_parameters":
            data = self.bridge.parameters(rid)
        elif name == "sample_motion":
            data = self.bridge.sample(rid)
        elif name == "measure_topics":
            data = self.bridge.topic_health(rid)
        elif name == "inspect_transforms":
            data = self.bridge.transform_health(rid)
        else:
            raise ValueError("Unknown read-only tool")
        event = self.store.event_v2(r, "tool_call", name, {
            "tool": name,
            "actor": (
                "deterministic_rule" if r.get("mode") == "rules"
                else "diagnostic_agent"
            ),
            "input_summary": {"robot_id": rid},
            "output_summary": data,
            "evidence_id": None,
            "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
            "success": True,
            "error": None,
        }, step_id=r.get("current_step_id"))
        # The model-facing view stays compact and compatible with the v1 tool
        # protocol while its evidence ID points at the persisted v2 event.
        return {
            "id": event["id"],
            "time": event["time"],
            "kind": "tool_call",
            "title": name,
            "data": data,
        }

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
        no_tools = r["mode"] == "no_tools_llm"

        def collect(name):
            e = self.tool(name, r)
            evidence[e["id"]] = e
            return e

        # The no-tools ablation receives exactly the failed navigation record.
        # Other strategies start with a server-captured graph observation.
        if no_tools:
            initial = None
            navigation_events = [
                event for event in r.get("events", [])
                if event.get("kind") == "navigation_attempt"
            ]
            if not navigation_events:
                raise ModelError("No-tools diagnosis requires a navigation attempt")
            navigation = navigation_events[-1]
            evidence[navigation["id"]] = navigation
        else:
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
        system = DIAGNOSTIC_SYSTEM_PROMPT.format(robot_id=r["robot_id"])
        messages = [
            {
                "role": "system",
                "content": system,
            }
        ]
        user_payload = (
            no_tools_input(
                r["robot_id"], r["prompt"], r["target"], navigation
            )
            if no_tools
            else {
                "request": r["prompt"],
                "baseline": r["baseline"],
                "initial_evidence": initial,
            }
        )
        messages.append({
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False),
        })
        available_tools = [TOOLS[-1]] if no_tools else TOOLS
        protocol_retries = 0
        for step in range(6):
            if cancel.is_set():
                raise Canceled()
            try:
                message, usage = model.complete(messages, available_tools)
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
            calls = message.get("tool_calls") or []
            self.store.event_v2(
                r,
                "agent_decision",
                "模型选择下一步调查",
                {
                    "agent": "diagnostic",
                    "mode": "no_tools_llm" if no_tools else "tools_llm",
                    "round": step + 1,
                    "requested_tools": [
                        call.get("function", {}).get("name", "invalid")
                        for call in calls
                    ],
                    "reason_summary": "模型按现场证据选择下一步只读调查。",
                    "usage_delta": {
                        "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                        "completion_tokens": int(usage.get("completion_tokens", 0)),
                    },
                },
                step_id=r.get("current_step_id"),
            )
            if not calls:
                self.store.event(
                    r, "warning", "模型未调用调查工具，压缩上下文后重试",
                    {"round": step + 1, "fallback": False},
                )
                messages.append(
                    {
                        "role": "user",
                        "content": "Use the available tools and finish_diagnosis; do not conclude in free text.",
                    }
                )
                continue
            if len(calls) > 5:
                raise ModelError("Model exceeded per-round tool limit")
            # Preserve protocol fields (including provider reasoning if
            # required) only for a valid tool turn. Free-text failures can be
            # very large and add no evidence to the next retry.
            messages.append(message)
            for call in calls:
                if cancel.is_set():
                    raise Canceled()
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments", "{}"))
                    if name == "finish_diagnosis":
                        required = {
                            "classification",
                            "known_cause",
                            "suspected_subsystem",
                            "summary",
                            "cited_evidence",
                            "ruled_out",
                            "repair_requested",
                            "requested_repair",
                            "recommended_action",
                        }
                        if (
                            not isinstance(args, dict)
                            or set(args) != required
                        ):
                            raise ValueError("Invalid diagnosis schema")
                        if (
                            not isinstance(args["cited_evidence"], list)
                            or not args["cited_evidence"]
                            or any(x not in evidence for x in args["cited_evidence"])
                        ):
                            raise ValueError(
                                "Citations must refer to observed evidence"
                            )
                        if not all(isinstance(args[k], str) and len(args[k]) <= 4000
                                   for k in ["summary", "recommended_action"]):
                            raise ValueError("Invalid explanation")
                        args.update(
                            schema_version=2,
                            strategy=("no_tools_llm" if no_tools else "tools_llm"),
                            cause=(
                                args["known_cause"]
                                if args["classification"] == "known_fault"
                                else ("healthy" if args["classification"] == "healthy" else "unknown")
                            ),
                            evidence_ids=list(args["cited_evidence"]),
                            repair_rationale=args["recommended_action"],
                        )
                        validate_diagnosis(args)
                        validate_diagnosis_evidence(
                            args, r["events"], r["id"], r["robot_id"],
                            diagnosed_at=time.time(),
                        )
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
    localization = graph.get("localization_health", {})
    if (
        localization.get("status") == "drifted"
        and isinstance(localization.get("position_error"), (int, float))
        and localization["position_error"] >= localization.get("threshold", 0.45)
    ):
        return "localization_drift"
    topics = graph.get("topics", {})
    if topics.get("raw_scan_hz", 0) > 0 and topics.get("nav2_scan_hz", 0) == 0:
        return "scan_interrupted"
    if topics.get("raw_scan_hz", 0) > 0 and topics.get("nav2_scan_hz", 0) > 0:
        return "healthy"
    return "unknown"
