import React from "react";
import type { Authorization, Diagnosis, Event, Run } from "./types";
import {
  actors, causes, classifications, clock, legacyStatus, operations, phaseLabels, phaseOf,
  phaseTone, robotName, strategies, subsystems, tokens,
} from "./format";

const PATH = ["navigating", "investigating", "repairing", "replaying", "verifying"];

export function PhaseBadge({ phase }: { phase: string }) {
  return <span className={"phase " + phaseTone(phase)}>{phaseLabels[phase] || legacyStatus[phase] || phase}</span>;
}

function Evidence({ ids, run }: { ids?: string[]; run: Run }) {
  if (!ids?.length) return null;
  return <span className="evidence">
    {ids.map((id) => <a key={id} href={"#" + run.id + id}>{id}</a>)}
  </span>;
}

/** Diagnostic path, derived from recorded phase transitions rather than guessed from titles. */
function Stepper({ run }: { run: Run }) {
  const phase = phaseOf(run);
  const visited = new Set(run.events.filter((e) => e.kind === "phase_changed").map((e) => e.data?.to));
  visited.add(phase);
  const terminal = ["verified", "nav2_ok_gazebo_failed", "failed", "escalated", "canceled"].includes(phase);
  return <ol className="stepper">
    {PATH.map((p) => {
      const state = phase === p ? "now" : visited.has(p) ? "done" : terminal ? "skip" : "todo";
      return <li key={p} className={state}><i />{phaseLabels[p]}</li>;
    })}
    <li className={"end " + (terminal ? phaseTone(phase) : "todo")}><i />{terminal ? phaseLabels[phase] : "结果"}</li>
  </ol>;
}

function DiagnosisCard({ d, run }: { d: Diagnosis; run: Run }) {
  if (!d.classification) {
    return <div className="card">
      <div className="card-head"><b>诊断结论</b><span>{causes[d.cause] || d.cause}</span></div>
      <p>{d.summary}</p>
      <small>{d.repair_rationale}</small>
      <Evidence ids={d.evidence_ids} run={run} />
    </div>;
  }
  return <div className="card">
    <div className="card-head">
      <b>诊断</b>
      <span className="muted">{strategies[d.strategy || ""] || d.strategy} · 只是建议，不等于授权</span>
    </div>
    <dl className="facts">
      <div><dt>分类</dt><dd className={"cls " + d.classification}>{classifications[d.classification]}</dd></div>
      <div><dt>{d.known_cause ? "已知原因" : "疑似子系统"}</dt>
        <dd>{d.known_cause ? causes[d.known_cause] : subsystems[d.suspected_subsystem || "unknown"]}</dd></div>
      <div><dt>申请修复</dt><dd>{d.repair_requested ? operations[d.requested_repair || ""] || d.requested_repair : "否"}</dd></div>
    </dl>
    <p>{d.summary}</p>
    <div className="cite">引用证据 <Evidence ids={d.cited_evidence} run={run} /></div>
    {!!d.ruled_out?.length && <details className="ruled">
      <summary>已排除 {d.ruled_out.length} 个假设</summary>
      <ul>{d.ruled_out.map((r) => <li key={r.hypothesis}>
        <b>{r.hypothesis}</b> {r.reason} <Evidence ids={r.evidence_ids} run={run} />
      </li>)}</ul>
    </details>}
  </div>;
}

function AuthorizationCard({ a, run }: { a: Authorization; run: Run }) {
  const decision = { authorized: "已授权", denied: "已拒绝", not_applicable: "不适用" }[a.decision];
  return <div className={"card auth " + a.decision}>
    <div className="card-head">
      <b>修复授权</b>
      <span className="muted">确定性程序 · {a.rule_version} · 证据 ≤ {a.max_evidence_age_seconds}s</span>
    </div>
    <dl className="facts">
      <div><dt>决定</dt><dd className={"decision " + a.decision}>{decision}</dd></div>
      <div><dt>申请动作</dt><dd>{a.requested_operation ? operations[a.requested_operation] || a.requested_operation : "无"}</dd></div>
      <div><dt>复测证据</dt><dd><Evidence ids={a.evidence_ids} run={run} /></dd></div>
    </dl>
    <p>{a.reason}</p>
    {!!a.checks.length && <table className="checks"><tbody>
      {a.checks.map((c) => <tr key={c.name}>
        <td className={c.passed ? "pass" : "fail"}>{c.passed ? "✓" : "✕"}</td>
        <td><code>{c.name}</code></td>
        <td>期望 <code>{String(c.expected ?? "—")}</code></td>
        <td>实测 <code>{String(c.actual ?? "—")}</code></td>
      </tr>)}
    </tbody></table>}
  </div>;
}

function summarize(value: unknown) {
  if (value == null) return "";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 96 ? text.slice(0, 96) + "…" : text;
}

function EventRow({ e, run, cited }: { e: Event; run: Run; cited: Set<string> }) {
  const d = e.data || {};
  const id = <code className="eid">{e.id}</code>;
  const time = <time>{clock(e.time)}</time>;
  if (e.kind === "phase_changed") {
    return <li id={run.id + e.id} className="ev phase-row">
      {id}<span>→ {phaseLabels[d.to] || d.to}</span><small>{d.reason}</small>{time}
    </li>;
  }
  let body: React.ReactNode = <b>{e.title}</b>;
  if (e.kind === "tool_call") {
    body = <><span className={"actor " + d.actor}>{actors[d.actor] || d.actor}</span>
      <b className="mono">{d.tool}</b>{!d.success && <em className="bad">失败</em>}
      {d.duration_ms != null && <small>{d.duration_ms} ms</small>}</>;
  } else if (e.kind === "agent_decision") {
    body = <><span className="actor model">模型 · 第 {d.round} 轮</span>
      <b>请求 {(d.requested_tools || []).join("、")}</b>
      <small>{tokens(d.usage_delta)} tokens</small></>;
  } else if (e.kind === "navigation_attempt") {
    body = <><span className="actor nav">Nav2</span>
      <b>{e.title}</b><em className={d.nav2_success ? "ok" : "bad"}>{d.nav2_success ? "成功" : "失败"}</em>
      {!d.nav2_success && <small>{d.nav2_status}</small>}</>;
  } else if (e.kind === "verification") {
    body = <><span className="actor truth">Gazebo</span><b>独立验收</b>
      <em className={d.gazebo_success ? "ok" : "bad"}>{d.gazebo_success ? "通过" : "未通过"}</em>
      {d.position_error_m != null && <small>误差 {d.position_error_m.toFixed(3)} m</small>}</>;
  } else if (e.kind === "escalation") {
    body = <><span className="actor warn">升级</span><b>{e.title}</b></>;
  } else if (e.kind === "repair_execution") {
    body = <><span className="actor repair">修复</span><b className="mono">{d.operation}</b>
      <em className={d.success ? "ok" : "bad"}>{d.success ? "执行成功" : "执行失败"}</em></>;
  }
  const detail = e.kind === "tool_call" ? d.output_summary : d;
  return <li id={run.id + e.id} className={"ev " + (cited.has(e.id) ? "cited" : "")}>
    <details>
      <summary>{id}<span className="ev-body">{body}</span>
        {cited.has(e.id) && <span className="cited-tag">被诊断引用</span>}{time}</summary>
      {e.kind === "tool_call" && <p className="io">输出摘要：{summarize(d.output_summary)}</p>}
      <pre>{JSON.stringify(detail, null, 2) || "事件已记录"}</pre>
    </details>
  </li>;
}

export default function RunDetail({ run }: { run: Run }) {
  const phase = phaseOf(run);
  const d = run.diagnosis;
  const cited = new Set(d?.cited_evidence || d?.evidence_ids || []);
  const tools = run.events.filter((e) => e.kind === "tool_call");
  const diagTools = tools.filter((e) => e.data?.actor !== "repair_authorizer").length;
  const escalation = [...run.events].reverse().find((e) => e.kind === "escalation");
  return <div className="run-detail">
    <div className="run-title">
      <div>
        <h3>{robotName(run.robot_id)} <span className="muted">→ ({(run.target || run.active_target || [2, 0.5]).join(", ")})</span></h3>
        <small className="mono">{run.id} · {d?.strategy ? strategies[d.strategy] : run.mode === "ai" ? "AI" : "规则"}
          {run.backend && run.backend !== "rules" ? ` · ${run.backend}` : ""}
          {run.prompt_version ? ` · ${run.prompt_version}` : ""}</small>
      </div>
      <PhaseBadge phase={phase} />
    </div>
    <Stepper run={run} />
    <div className="numbers">
      <div><small>首次导航</small><b>{run.baseline ? run.baseline.success ? "到达" : "失败" : "—"}</b></div>
      <div><small>诊断工具调用</small><b>{diagTools}</b></div>
      <div><small>tokens</small><b>{tokens(run.usage).toLocaleString()}</b></div>
      <div><small>Gazebo 误差 · 阈值 0.35</small><b>{run.validation ? `${run.validation.error.toFixed(3)} m` : "—"}</b></div>
    </div>
    {phase === "escalated" && <div className="callout warn" role="status">
      <b>未执行自动修复，等待人工处理</b>
      <p>{escalation?.data?.recommended_action || d?.recommended_action || "当前证据不支持任何白名单修复。"}</p>
    </div>}
    {phase === "nav2_ok_gazebo_failed" && <div className="callout warn" role="status">
      <b>Nav2 报告成功，但 Gazebo 真值未通过验收</b>
      <p>导航 action 的返回值不作为最终结果；以 Gazebo 里程计独立判定。</p>
    </div>}
    {run.error && <div className="callout bad"><b>执行错误</b><p>{run.error}</p></div>}
    {run.plan && <div className="card">
      <div className="card-head"><b>Assistant 分派</b><span className="muted">规划 tokens {tokens(run.plan.usage)}</span></div>
      <p>{run.plan.message}</p>
    </div>}
    {d && <DiagnosisCard d={d} run={run} />}
    {run.repair_authorization && <AuthorizationCard a={run.repair_authorization} run={run} />}
    <div className="timeline-head">
      <b>证据时间线</b><small>{run.events.length} 条事件 · 点击展开原始数据</small>
    </div>
    <ol className="timeline">
      {run.events.map((e) => <EventRow key={e.id} e={e} run={run} cited={cited} />)}
    </ol>
    <a className="export" href={`/api/runs/${run.id}/export`}>导出本次运行 JSON ↗</a>
  </div>;
}
