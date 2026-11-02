import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import SourceCase from "./SourceCase";
import Scene from "./Scene";
import RunDetail, { PhaseBadge } from "./RunDetail";
import Experiment from "./Experiment";
import type { AssistantPlan, State } from "./types";
import {
  causes, clock, isTerminal, legacyStatus, phaseOf, robotColor, robotName, robotShort, strategies, tokens,
} from "./format";

const FAULTS = [
  "scan_interrupted", "navigator_inactive", "controller_velocity_zero", "localization_drift",
  "transform_updates_stopped",
];

function useLabState() {
  const [data, setData] = useState<State | null>(null);
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    let poll: number | undefined;
    const s = new EventSource("/api/events");
    s.onmessage = (e) => {
      setData(JSON.parse(e.data));
      setConnected(true);
      window.clearInterval(poll);
      poll = undefined;
    };
    // Contract v2 §8: fall back to polling /api/state while SSE is down.
    s.onerror = () => {
      setConnected(false);
      if (poll === undefined) {
        poll = window.setInterval(() => {
          fetch("/api/state").then((r) => r.json()).then(setData).catch(() => undefined);
        }, 2000);
      }
    };
    return () => {
      s.close();
      window.clearInterval(poll);
    };
  }, []);
  return { data, connected };
}

function PlanResult({ plan, onOpen }: { plan: AssistantPlan; onOpen: (id: string) => void }) {
  const clarifying = plan.status === "needs_clarification";
  const used = tokens(plan.usage);
  const sa = plan.spatial_analysis;
  const fa = plan.fleet_acceptance;
  return <div className="plan" role="status">
    <div className="plan-head">
      <b>{plan.run_ids.length ? "已分派" : clarifying ? "需要补充" : plan.status === "not_executable" ? "不可执行"
        : plan.status === "continued" ? "已由后续指令接续" : "未执行"}</b>
      <span className="muted">{plan.plan_type === "fleet" ? "车队 Agent" : robotName(plan.robot_id)}
        {" · "}{used === 0 ? "确定性解析，未调用模型" : `模型规划 · ${used} tokens`}</span>
    </div>
    <p>{plan.message}</p>
    {!!plan.suggested_requests?.length && <ul className="plain">
      {plan.suggested_requests.map((item) => <li key={item}>可改为：{item}</li>)}
    </ul>}
    {sa && <p className="small">
      空间分析 · {sa.objective === "maximize_separation" ? "最大安全分离" : sa.objective === "balanced_coverage" ? "均衡覆盖" : "安全会合"}
      {` · 目标间距 ${sa.metrics.target_separation.toFixed(2)} m · Nav2 总路径 ${sa.metrics.total_nav2_path_length.toFixed(2)} m`}
      {` · ${sa.evidence.safe_candidate_count} 个安全候选点（0.40 m 采样，近似解）`}
    </p>}
    {fa && <p className="small">
      {fa.success ? "空间目标验收通过" : "空间目标验收未通过"}
      {fa.actual_separation != null ? ` · Gazebo 实测间距 ${fa.actual_separation.toFixed(2)} m` : ""}
    </p>}
    {plan.handoff_policy_reason && <p className="small">接替策略：{plan.handoff_policy_reason}</p>}
    {(plan.steps?.length || plan.assignments?.length) ? <ol className="steps">
      {(plan.steps || []).map((s) => <li key={s.id}>
        <code>{s.id}</code> {s.description}
        {s.x !== undefined && s.y !== undefined && <span className="muted"> ({s.x}, {s.y})</span>}
      </li>)}
      {(plan.assignments || []).flatMap((a) => a.steps.map((s) => <li key={a.robot_id + s.id}>
        <code style={{ color: robotColor(a.robot_id) }}>{robotShort(a.robot_id)}</code> {s.description}
      </li>))}
    </ol> : null}
    {(plan.handoffs || []).map((h) => <p className="small" key={h.handoff_run_id}>
      接替 {robotShort(h.source_robot_id)} → {robotShort(h.target_robot_id)} · {legacyStatus[h.status] || h.status}
      {h.final_gazebo_error != null ? ` · Gazebo 误差 ${h.final_gazebo_error.toFixed(3)} m` : ""}
      <button className="link" onClick={() => onOpen(h.handoff_run_id)}>查看</button>
    </p>)}
    {!!plan.turns?.length && <details><summary>指令对话</summary>
      {plan.turns.map((t, i) => <p key={i} className="small"><b>{t.role === "user" ? "你" : "Assistant"}：</b>{t.content}</p>)}
    </details>}
    {plan.run_ids[0] && <button className="link" onClick={() => onOpen(plan.run_ids[0])}>查看执行过程 ↓</button>}
  </div>;
}

function App() {
  const { data, connected } = useLabState();
  const [tab, setTab] = useState<"task" | "fault">("task"),
    [robot, setRobot] = useState("robot_1"),
    [selected, setSelected] = useState(""),
    [mode, setMode] = useState("ai"),
    [fault, setFault] = useState("scan_interrupted"),
    [prompt, setPrompt] = useState("排查为什么机器人无法到达货架 A。请逐步收集现场证据，并验证修复效果。"),
    [repair, setRepair] = useState(true),
    [busy, setBusy] = useState(false),
    [instruction, setInstruction] = useState(() => (localStorage.getItem("rdl.plan") ? "" : "让机器人去货架 A")),
    [missionRepair, setMissionRepair] = useState(true),
    [fleetHandoff, setFleetHandoff] = useState(true),
    [missionScope, setMissionScope] = useState<"single" | "fleet">("single"),
    [planning, setPlanning] = useState(false),
    [planId, setPlanId] = useState(() => localStorage.getItem("rdl.plan") || ""),
    [returnedPlan, setReturnedPlan] = useState<AssistantPlan | null>(null),
    [assistantError, setAssistantError] = useState(""),
    [notice, setNotice] = useState("");

  useEffect(() => {
    if (data?.robots.length && !data.robots.some((item) => item.id === robot)) setRobot(data.robots[0].id);
  }, [data, robot]);

  const runs = data?.runs || [];
  const run = runs.find((r) => r.id === selected) || runs[0];
  const live = runs.filter((r) => !isTerminal(r));
  const assistantPlan = data?.plans?.find((p) => p.id === planId) || returnedPlan;
  const clarifying = assistantPlan?.status === "needs_clarification";
  const chosen = data?.robots.find((r) => r.id === robot);
  const disabled = busy || !connected || chosen?.busy;

  function open(id: string) {
    setSelected(id);
    document.getElementById("investigation")?.scrollIntoView({ behavior: "smooth" });
  }
  function choosePlan(id: string) {
    setAssistantError("");
    setPlanId(id);
    setReturnedPlan(null);
    localStorage.setItem("rdl.plan", id);
    setInstruction("");
  }
  function newInstruction(text = "") {
    choosePlan("");
    setMissionRepair(true);
    setInstruction(text);
  }
  async function post(path: string, body: unknown = {}, onError?: (message: string) => void) {
    if (!data) return;
    setBusy(true);
    setNotice("");
    try {
      const r = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Lab-Token": data.token },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (!r.ok) throw Error(typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail));
      return d;
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      if (onError) onError(message);
      else setNotice(message);
      return null;
    } finally {
      setBusy(false);
    }
  }
  async function diagnose() {
    const d = await post("/api/runs", { robots: [robot], mode, prompt, auto_repair: repair });
    if (d) setSelected(d.run_ids[0]);
  }
  async function executeInstruction() {
    const parentPlan = assistantPlan;
    setAssistantError("");
    if (!clarifying) {
      setPlanId("");
      setReturnedPlan(null);
      localStorage.removeItem("rdl.plan");
    }
    setPlanning(true);
    try {
      const d = missionScope === "fleet"
        ? await post("/api/fleet/tasks", {
          prompt: instruction, auto_repair: missionRepair, allow_handoff: fleetHandoff,
        }, setAssistantError)
        : await post("/api/assistant/tasks", {
          prompt: instruction, auto_repair: missionRepair,
          parent_id: clarifying ? parentPlan?.id : null,
          robot_id: clarifying ? parentPlan?.robot_id : robot,
        }, setAssistantError);
      if (d) {
        setPlanId(d.plan.id);
        localStorage.setItem("rdl.plan", d.plan.id);
        setReturnedPlan(d.plan);
        setInstruction("");
        if (d.run_ids.length) setSelected(d.run_ids[0]);
      }
    } finally {
      setPlanning(false);
    }
  }

  return <>
    <header className="topbar">
      <a className="brand" href="/">Robot Debug Lab</a>
      <nav>
        <a href="#scene">现场</a>
        <a href="#investigation">调查记录 <small>{runs.length}</small></a>
        <a href="#experiment">实验</a>
        <a href="#source-case">源码案例</a>
      </nav>
      <div className="env">
        <span className={"conn " + (connected ? "on" : "")}>{connected ? "实时连接正常" : "正在连接实验环境"}</span>
        <span>Gazebo + Nav2 仿真</span>
        <span>ROS domain {data?.ros_domain || "—"}</span>
        <span>{data?.runtime === "openfang" ? "OpenFang" : "直连"} · {data?.model === "default" ? "默认模型" : data?.model || "—"}</span>
      </div>
    </header>

    <main>
      <section className="intro">
        <h1>导航失败时，让 Agent 查证据；<br />能不能修，由确定性程序决定。</h1>
        <p>两台 TurtleBot3 在 Gazebo 仓库中执行真实 Nav2 任务。Agent 只读调查 ROS 现场并引用证据；
          修复必须通过白名单和新鲜复测；结果以 Gazebo 真值独立验收。</p>
      </section>

      {notice && <div role="status" className="notice">
        {notice}<button aria-label="关闭提示" onClick={() => setNotice("")}>×</button>
      </div>}

      <div className="stage">
        <section id="scene" className="field">
          <div className="field-head">
            <h2>现场 <small>Nav2 占据栅格 · Gazebo 里程计</small></h2>
            <span className="muted small">
              路线协调：{data?.traffic?.active.length || 0} 条已占用 · {data?.traffic?.queued || 0} 条等待
              {data?.traffic ? ` · 安全间距 ${data.traffic.clearance.toFixed(2)} m` : ""}
            </span>
          </div>
          <div className="map"><Scene robots={data?.robots || []} activeTargets={data?.active_targets || {}} selected={robot} /></div>
          <div className="robots">
            {(data?.robots || []).map((r) => {
              const current = live.find((x) => x.robot_id === r.id);
              return <button key={r.id} className={"robot-card " + (robot === r.id ? "selected" : "")}
                style={{ "--c": robotColor(r.id) } as React.CSSProperties} onClick={() => setRobot(r.id)}>
                <span className="swatch" />
                <span className="rc-name"><b>{robotName(r.id)}</b>
                  <small>{r.controller_alive ? "控制节点在线" : "控制节点离线"}</small></span>
                <span className="rc-pose mono">{r.pose ? `${r.pose.x.toFixed(2)}, ${r.pose.y.toFixed(2)}` : "—"}</span>
                <span className="rc-meta small">
                  雷达 {r.scan ? `${r.scan.raw_scan_hz.toFixed(0)}→${r.scan.nav2_scan_hz.toFixed(0)} Hz` : "—"}
                  {" · "}AMCL {r.localization?.position_error != null ? `${r.localization.position_error.toFixed(2)} m` : "—"}
                </span>
                {current ? <PhaseBadge phase={phaseOf(current)} /> : <span className="phase muted">空闲</span>}
              </button>;
            })}
          </div>
        </section>

        <section className="console">
          <div className="tabs" role="tablist">
            <button role="tab" aria-selected={tab === "task"} className={tab === "task" ? "on" : ""}
              onClick={() => setTab("task")}>自然语言任务</button>
            <button role="tab" aria-selected={tab === "fault"} className={tab === "fault" ? "on" : ""}
              onClick={() => setTab("fault")}>故障实验</button>
          </div>

          {tab === "task" ? <div className="pane">
            <div className="segmented">
              <button className={missionScope === "single" ? "chosen" : ""} disabled={clarifying}
                onClick={() => setMissionScope("single")}>所选机器人</button>
              <button className={missionScope === "fleet" ? "chosen" : ""} disabled={clarifying}
                onClick={() => setMissionScope("fleet")}>车队协同 Agent</button>
            </div>
            <div className="row-actions">
              <button className="chip" disabled={busy} onClick={() => newInstruction("让机器人去货架 A")}>导航示例</button>
              <button className="chip" disabled={busy} onClick={() => newInstruction()}>新建指令</button>
            </div>
            {clarifying && <p className="callout warn small">{assistantPlan.message}</p>}
            <label htmlFor="instruction">{clarifying ? "补充信息（保留上文的机器人与要求）"
              : missionScope === "fleet" ? "告诉车队要做什么" : `告诉 ${robotName(robot)} 去哪里`}</label>
            <textarea id="instruction" value={instruction} maxLength={2000} rows={3}
              onChange={(e) => setInstruction(e.target.value)}
              placeholder={missionScope === "fleet" ? "例如：让离货架 A 最近的机器人去检查，另一台去充电站等待" : "例如：让机器人去货架 A"} />
            <label className="checkbox">
              <input type="checkbox" checked={missionRepair && !(clarifying && assistantPlan.auto_repair_ceiling === false)}
                disabled={clarifying && assistantPlan.auto_repair_ceiling === false}
                onChange={(e) => setMissionRepair(e.target.checked)} />
              导航失败后，允许执行限定修复并重试原目标
            </label>
            {missionScope === "fleet" && <label className="checkbox">
              <input type="checkbox" checked={fleetHandoff} onChange={(e) => setFleetHandoff(e.target.checked)} />
              一台机器人仍无法完成时，允许健康机器人接替剩余可迁移步骤
            </label>}
            <button className="primary" onClick={executeInstruction}
              disabled={busy || planning || !connected || !instruction.trim()}>
              {planning ? "Assistant 正在解析任务…" : clarifying ? "补充并继续" : "解析并执行任务"}
            </button>
            {assistantError && <div className="callout bad" role="alert">
              <b>本次任务未分派</b><p>{assistantError}</p><small>机器人没有收到运动命令。</small>
            </div>}
            {assistantPlan && <PlanResult plan={assistantPlan} onOpen={open} />}
            <label htmlFor="plan-history">指令历史（选择后只查看，不会再次执行）</label>
            <select id="plan-history" value={planId} disabled={busy} onChange={(e) => choosePlan(e.target.value)}>
              <option value="">新指令</option>
              {(data?.plans || []).map((p) => <option value={p.id} key={p.id}>
                {p.status === "needs_clarification" ? "待补充" : p.status === "dispatched" ? "已分派" : p.status === "continued" ? "已续接" : "未执行"}
                {" · "}{robotShort(p.robot_id)} · {(p.prompt || p.message || p.id).slice(0, 40)}
              </option>)}
            </select>
          </div> : <div className="pane">
            <p className="small muted">向 {robotName(robot)} 注入故障，再让它去货架 A (2, 0.5)。
              故障标签不进入 Agent 上下文，只写入私有评测表。</p>
            <label htmlFor="fault">1 · 注入故障</label>
            <div className="inline">
              <select id="fault" aria-label="故障类型" value={fault} onChange={(e) => setFault(e.target.value)}>
                {FAULTS.map((k) => <option value={k} key={k}>
                  {causes[k]}{k === "transform_updates_stopped" ? "（未见，无白名单修复）" : ""}
                </option>)}
              </select>
              <button disabled={disabled} onClick={async () => {
                if (await post(`/api/robots/${robot}/fault`, { kind: fault })) setNotice("已注入故障，现在开始诊断。");
              }}>注入</button>
              <button className="quiet" disabled={disabled} onClick={async () => {
                if (await post(`/api/robots/${robot}/reset`)) setNotice("Gazebo 世界已重置，两台机器人回到起点。");
              }}>重置</button>
            </div>
            <label>2 · 诊断策略</label>
            <div className="segmented">
              <button className={mode === "ai" ? "chosen" : ""} onClick={() => setMode("ai")}>模型 AI</button>
              <button className={mode === "rules" ? "chosen" : ""} onClick={() => setMode("rules")}>规则基线</button>
            </div>
            <label htmlFor="request">3 · 排查请求</label>
            <textarea id="request" rows={3} value={prompt} maxLength={2000} onChange={(e) => setPrompt(e.target.value)} />
            <label className="checkbox">
              <input type="checkbox" checked={repair} onChange={(e) => setRepair(e.target.checked)} />
              授权通过时执行白名单修复并重放原目标
            </label>
            <button className="primary" disabled={disabled || !prompt.trim() || (mode === "ai" && !data?.ai_configured)}
              onClick={diagnose}>{busy ? "正在提交…" : "开始诊断"}</button>
            <button className="quiet wide" disabled={disabled} onClick={async () => {
              const d = await post(`/api/robots/${robot}/navigate`);
              if (d) setNotice(d.success ? "移动任务通过位置验收" : `移动失败：${d.message}`);
            }}>仅执行目标点任务</button>
          </div>}
        </section>
      </div>

      <section id="investigation" className="block">
        <div className="block-head">
          <div><span className="kicker">调查记录</span><h2>每一步都能追到证据编号</h2></div>
          {run && !isTerminal(run) && <button disabled={busy} onClick={async () => {
            const result = await post(`/api/runs/${run.id}/cancel`);
            if (result) setNotice(result.accepted ? "已请求取消，正在停止任务并释放机器人。" : "该任务已结束。");
          }}>取消当前任务</button>}
        </div>
        <div className="investigation">
          <ul className="run-list">
            {runs.length ? runs.map((r) => <li key={r.id}>
              <button className={run?.id === r.id ? "current" : ""} onClick={() => setSelected(r.id)}>
                <span className="rl-top">
                  <i style={{ background: robotColor(r.robot_id) }} />
                  <b>{robotShort(r.robot_id)}</b>
                  <span className="muted">{r.diagnosis?.strategy ? strategies[r.diagnosis.strategy] : r.mode === "ai" ? "AI" : "规则"}</span>
                  <time>{clock(r.created)}</time>
                </span>
                <PhaseBadge phase={phaseOf(r)} />
              </button>
            </li>) : <li className="muted small">还没有运行记录</li>}
          </ul>
          {run ? <RunDetail run={run} /> : <div className="empty">
            <h3>从一个可复现的问题开始</h3>
            <p>在「故障实验」中注入故障并开始诊断，工具调用、证据和验收结果会出现在这里。</p>
          </div>}
        </div>
      </section>

      <Experiment />
      <SourceCase />
    </main>
    <footer>Robot Debug Lab · Gazebo 现场 · Nav2 导航 · Agent 调查 · 受限修复 · 独立验收</footer>
  </>;
}

createRoot(document.getElementById("root")!).render(<App />);
