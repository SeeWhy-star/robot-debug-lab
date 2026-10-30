import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import SourceCase from "./SourceCase";
type Pose = { x: number; y: number; theta: number; speed: number };
type Robot = {
  id: string;
  pose?: Pose;
  trace: number[][];
  controller_alive: boolean;
  busy: boolean;
  runtime?: string;
  scan?: { raw_scan_hz: number; nav2_scan_hz: number; scan_forwarding: boolean };
};
type Event = {
  id: string;
  time: number;
  kind: string;
  title: string;
  data: unknown;
};
type Verdict = {
  success: boolean;
  error: number;
  elapsed: number;
  message: string;
};
type Run = {
  target?: number[];
  task_type?: string;
  plan?: AssistantPlan;
  backend?: string;
  id: string;
  robot_id: string;
  mode: string;
  status: string;
  created: number;
  events: Event[];
  diagnosis?: {
    cause: string;
    summary: string;
    evidence_ids: string[];
    repair_rationale: string;
  };
  baseline?: Verdict;
  validation?: Verdict;
  usage: { prompt_tokens: number; completion_tokens: number };
  error?: string;
  active_target?: number[] | null;
  steps?: MissionStep[];
};
type MissionStep = {
  id: string;
  action: string;
  description: string;
  destination_id?: string;
  x?: number;
  y?: number;
  duration_seconds?: number;
  auto_repair: boolean;
  status: string;
};
type AssistantPlan = {
  id: string;
  status: string;
  message: string;
  steps?: MissionStep[];
  usage?: { prompt_tokens: number; completion_tokens: number };
  run_ids: string[];
  prompt?: string;
  auto_repair_ceiling?: boolean;
  turns?: { role: string; content: string }[];
};
type State = {
  plans?: AssistantPlan[];
  runtime?: string;
  simulation?: string;
  robots: Robot[];
  runs: Run[];
  active_targets?: Record<string, number[]>;
  model: string;
  ai_configured: boolean;
  token: string;
  ros_domain: string;
};
const labels: Record<string, string> = {
  queued: "排队",
  reproducing: "复现故障",
  navigating: "正在导航",
  investigating: "调查中",
  repairing: "执行修复",
  validating: "独立验收",
  waiting: "等待中",
  simulated_charging: "仿真充电中",
  idle: "空闲",
  verified: "验证通过",
  unresolved: "尚未解决",
  failed: "执行失败",
  canceled: "已取消",
  diagnosed: "诊断完成",
  interrupted: "任务中断",
};
const causes: Record<string, string> = {
  scan_interrupted: "导航雷达数据中断",
  navigator_inactive: "BT Navigator 生命周期停用",
  controller_velocity_zero: "Controller 速度配置为零",
  healthy: "状态正常",
  unknown: "证据不足",
};
const active = (r: Run) =>
  [
    "queued",
    "reproducing",
    "navigating",
    "investigating",
    "repairing",
    "validating",
    "waiting",
    "simulated_charging",
  ].includes(r.status);
function Scene({ robots, activeTargets }: { robots: Robot[]; activeTargets: Record<string, number[]> }) {
  // Exact transform for the Nav2 map: 10 m × 8 m, origin (-5, -4).
  const sx = (x: number) => 315 + x * 50;
  const sy = (y: number) => 225 - y * 50;
  return (
    <svg
      viewBox="0 0 600 470"
      role="img"
      aria-label="Nav2 实际占据栅格、机器人实时轨迹和目标位置"
    >
      <defs>
        <pattern id="grid" width="40" height="35" patternUnits="userSpaceOnUse">
          <path
            d="M 40 0 H 0 V 35"
            fill="none"
            stroke="#24373c"
            strokeWidth=".6"
          />
        </pattern>
      </defs>
      <rect x="65" y="25" width="500" height="400" fill="url(#grid)" />
      <image
        href="/assets/rdl-warehouse-map.png"
        x="65"
        y="25"
        width="500"
        height="400"
        preserveAspectRatio="none"
      />
      <path d="M65 25 V425 H565" fill="none" stroke="#628087" />
      {[-4, -2, 0, 2, 4].map((n) => (
        <g key={n}>
          <text x={sx(n)} y="447" fill="#6d8a91" fontSize="11">
            {n}
          </text>
          <text x="15" y={sy(n)} fill="#6d8a91" fontSize="11">
            {n}
          </text>
        </g>
      ))}
      <g fontSize="10" fontWeight="600">
        <text x={sx(3.2) - 18} y={sy(.5) - 5} fill="#8fe5d2">实体货架 A</text>
        <text x={sx(.55) - 25} y={sy(-.5) - 24} fill="#ef9a7f">实体障碍箱</text>
        <text x={sx(-4.3) - 12} y={sy(-3.15) - 18} fill="#7edba9">充电站</text>
      </g>
      {robots.map((r, i) => {
        const goal = activeTargets[r.id];
        if (!goal) return null;
        const color = i ? "#aeb9ff" : "#62e0c1";
        return <g key={r.id + "goal"} transform={`translate(${sx(goal[0])},${sy(goal[1])})`}>
          <circle r={i ? 19 : 23} fill="none" stroke={color} strokeDasharray="4 4" />
          <path d="M-7 0H7M0-7V7" stroke={color} />
          <text x="-10" y={i ? 42 : -30} fill={color} fontSize="10">
            {r.id.replace("robot_", "R0")} 目标 ({goal[0]}, {goal[1]})
          </text>
        </g>;
      })}
      {robots.map((r, i) => {
        const color = i ? "#aeb9ff" : "#62e0c1";
        const p = r.pose;
        return (
          <g key={r.id}>
            <polyline
              points={r.trace
                .map(([x, y]) => `${sx(x)},${sy(y)}`)
                .join(" ")}
              fill="none"
              stroke={color}
              strokeWidth="2"
              opacity=".65"
            />
            {p && (
              <g transform={`translate(${sx(p.x)},${sy(p.y)})`}>
                <circle r="17" fill={color} opacity=".13" />
                <path
                  d="M12 0L-8 -7L-5 0L-8 7Z"
                  fill={color}
                  transform={`rotate(${(-p.theta * 180) / Math.PI})`}
                />
                <text x="18" y={i ? 20 : -15} fill={color} fontSize="12">
                  {r.id.replace("robot_", "R0")}
                </text>
              </g>
            )}
          </g>
        );
      })}
      <text x="76" y="48" fill="#8ba4aa" fontSize="10">
        LIVE ROS 2 TELEMETRY · ACTUAL NAV2 OCCUPANCY MAP
      </text>
    </svg>
  );
}
function App() {
  const [data, setData] = useState<State | null>(null),
    [connected, setConnected] = useState(false),
    [robot, setRobot] = useState("robot_1"),
    [selected, setSelected] = useState(""),
    [mode, setMode] = useState("ai"),
    [fault, setFault] = useState("scan_interrupted"),
    [prompt, setPrompt] = useState(
      "排查为什么机器人无法到达货架 A。请逐步收集现场证据，并验证修复效果。",
    ),
    [repair, setRepair] = useState(true),
    [busy, setBusy] = useState(false),
    [instruction, setInstruction] = useState(() => localStorage.getItem("rdl.plan") ? "" : "让机器人去货架 A"),
    [missionRepair, setMissionRepair] = useState(true),
    [planning, setPlanning] = useState(false),
    [planId, setPlanId] = useState(() => localStorage.getItem("rdl.plan") || ""),
    [returnedPlan, setReturnedPlan] = useState<AssistantPlan | null>(null),
    [notice, setNotice] = useState("");
  useEffect(() => {
    const s = new EventSource("/api/events");
    s.onmessage = (e) => {
      setData(JSON.parse(e.data));
      setConnected(true);
    };
    s.onerror = () => setConnected(false);
    return () => s.close();
  }, []);
  useEffect(() => {
    if (data?.robots.length && !data.robots.some((item) => item.id === robot)) {
      setRobot(data.robots[0].id);
    }
  }, [data, robot, fault]);
  const run = data?.runs.find((r) => r.id === selected) || data?.runs[0];
  const assistantPlan = data?.plans?.find((p) => p.id === planId) || returnedPlan;
  const clarifying = assistantPlan?.status === "needs_clarification";
  function choosePlan(id: string) {
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
  async function post(path: string, body: unknown = {}) {
    if (!data) return;
    setBusy(true);
    setNotice("");
    try {
      const r = await fetch(path, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Lab-Token": data.token,
        },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (!r.ok)
        throw Error(
          typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail),
        );
      return d;
    } catch (e) {
      setNotice(String(e));
      return null;
    } finally {
      setBusy(false);
    }
  }
  async function diagnose() {
    const d = await post("/api/runs", {
      robots: [robot],
      mode,
      prompt,
      auto_repair: repair,
    });
    if (d) setSelected(d.run_ids[0]);
  }
  async function executeInstruction() {
    setPlanning(true);
    try {
      const d = await post("/api/assistant/tasks", {
        prompt: instruction, auto_repair: missionRepair,
        parent_id: clarifying ? assistantPlan.id : null,
      });
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
  const chosen = data?.robots.find((r) => r.id === robot);
  const disabled =
    busy ||
    !connected ||
    chosen?.busy;
  return (
    <div className="shell">
      <aside>
        <a className="brand" href="/">
          ◈
          <span>
            ROBOT
            <br />
            <b>DEBUG LAB</b>
          </span>
        </a>
        <div className="nav active">
          ◉ <span>调试工作台</span>
        </div>
        <a className="nav" href="#investigation">
          ⌁ <span>实验与证据</span>
          <small>{data?.runs.length || 0}</small>
        </a>
        <div className="aside-note">
          LOCAL LAB / 01
          <br />
          ROS 2 Lyrical
          <br />
          <span>Gazebo + Nav2 仿真</span>
        </div>
        <div className="connection">
          <i className={connected ? "on" : ""} />
          {connected ? "实时连接正常" : "正在连接实验环境"}
        </div>
      </aside>
      <main>
        <header>
          <div>
            <div className="eyebrow">
              EMBODIED AI / OBSERVE · DIAGNOSE · VERIFY
            </div>
            <h1>让每一次诊断，都有证据。</h1>
            <p>从真实 ROS 2 状态出发，复现问题、验证假设、检验修复。</p>
          </div>
          <div className="environment">
            <span className="pill">● 本地仿真</span>
            <small>
              {data?.model === "default" ? "OpenFang 默认模型" : data?.model || "模型连接中"}
              <br />
              ROS domain {data?.ros_domain || "—"}
              <br />
              调度后端：{data?.runtime === "openfang" ? "OpenFang" : "本地直连"}
            </small>
          </div>
        </header>
        <div className="robot-row">
          {data?.robots.map((r, i) => (
            <button
              className={"robot-card " + (robot === r.id ? "selected" : "")}
              key={r.id}
              onClick={() => setRobot(r.id)}
            >
              <span className={"robot-icon r" + i}>↗</span>
              <div>
                <b>{r.id.replace("robot_", "Robot 0")}</b>
                <span>
                  {r.busy
                    ? "任务执行中"
                    : r.controller_alive
                      ? "控制节点在线"
                      : "控制节点离线"}
                </span>
              </div>
              <div className="coords">
                {r.pose
                  ? `${r.pose.x.toFixed(2)}, ${r.pose.y.toFixed(2)}`
                  : "—"}
                <small>实时坐标</small>
              </div>
              <i className={r.controller_alive ? "on" : ""} />
            </button>
          ))}
        </div>
        {notice && (
          <div role="status" className="notice">
            {notice}
            <button aria-label="关闭提示" onClick={() => setNotice("")}>×</button>
          </div>
        )}
        <section className="panel command assistant-command">
          <div className="panel-head">
            <h2>Assistant 自然语言任务</h2>
            <span>描述目标 → 自动分派 → 失败诊断 → 验收</span>
          </div>
          <div className="task-examples">
            <button disabled={busy} onClick={() => newInstruction("让机器人去货架 A")}>导航示例</button>
            <button disabled={busy} onClick={() => newInstruction()}>新建指令</button>
          </div>
          <label htmlFor="plan-history">指令历史（选择后只查看，不会再次执行）</label>
          <select id="plan-history" value={planId} disabled={busy} onChange={(e) => choosePlan(e.target.value)}>
            <option value="">新指令</option>
            {(data?.plans || []).map((p) => <option value={p.id} key={p.id}>
              {p.status === "needs_clarification" ? "待补充" : p.status === "dispatched" ? "已分派" : p.status === "continued" ? "已续接" : "未执行"} · {(p.prompt || p.message || p.id).slice(0, 50)}
            </option>)}
          </select>
          {clarifying && <p className="hint clarification-question">{assistantPlan.message}</p>}
          <label htmlFor="instruction">{clarifying ? "补充信息（保留上文的机器人与要求）" : "告诉机器人去哪里"}</label>
          <textarea id="instruction" value={instruction} maxLength={2000}
            onChange={(e) => setInstruction(e.target.value)}
            placeholder="例如：让机器人去货架 A" />
          <label className="checkbox">
            <input type="checkbox" checked={missionRepair && !(clarifying && assistantPlan.auto_repair_ceiling === false)}
              disabled={clarifying && assistantPlan.auto_repair_ceiling === false}
              onChange={(e) => setMissionRepair(e.target.checked)} />
            导航失败后，允许执行限定修复并重试原目标
          </label>
          <button className="primary" onClick={executeInstruction}
            disabled={busy || planning || !connected || !instruction.trim()}>
            {planning ? "Assistant 正在解析任务…" : clarifying ? "补充并继续" : "解析并执行任务"}
          </button>
          <p className="hint">新指令只有点击上方按钮才会执行；历史下拉框只展示过去的计划。导航失败后，AI 会调查真实 ROS 状态。</p>
          {assistantPlan && <div className="assistant-result" role="status">
            <b>{assistantPlan.run_ids.length ? "已分派任务" : clarifying ? "请补充指令" : assistantPlan.status === "continued" ? "已由后续补充指令接续" : "计划未执行"}</b>
            <p>{assistantPlan.message}</p>
            {!!assistantPlan.turns?.length && <details><summary>查看指令对话</summary>
              {assistantPlan.turns.map((t, i) => <p key={i}><b>{t.role === "user" ? "你" : "Assistant"}：</b>{t.content}</p>)}
            </details>}
            {(assistantPlan.steps || []).map((step) => <p key={step.id}>
              {step.id} · {step.description}
              {step.x !== undefined && step.y !== undefined ? ` → (${step.x}, ${step.y})` : ""}
              {assistantPlan.run_ids[0] && <button className="quiet" onClick={() => {
                setSelected(assistantPlan.run_ids[0]);
                document.getElementById("investigation")?.scrollIntoView({ behavior: "smooth" });
              }}>查看执行结果</button>}
            </p>)}
            <small>规划后端：{(assistantPlan.usage?.prompt_tokens || 0) + (assistantPlan.usage?.completion_tokens || 0) === 0 ? "确定性解析（未调用模型）" : "模型结构化规划"} · tokens：{(assistantPlan.usage?.prompt_tokens || 0) + (assistantPlan.usage?.completion_tokens || 0)}</small>
          </div>}
        </section>
        <div className="workspace">
          <section className="panel scene">
            <div className="panel-head">
              <h2>运行现场</h2>
              <span>
                <i className={connected ? "on" : ""} /> LIVE
              </span>
            </div>
            <Scene robots={data?.robots || []} activeTargets={data?.active_targets || {}} />
            <div className="legend">
              <span>
                <i className="dot mint" /> Robot 01
              </span>
              <small>Gazebo 里程计轨迹 · 虚线圈仅表示当前执行目标，历史目标不显示</small>
            </div>
            <div className="fault-box">
              <div>
                <b>故障实验</b>
                <small>故障标签不进入诊断 Agent 上下文</small>
              </div>
              <select
                aria-label="故障类型"
                value={fault}
                onChange={(e) => setFault(e.target.value)}
              >
                {Object.entries(causes)
                  .filter(([key]) => !["healthy", "unknown"].includes(key))
                  .map(([k, v]) => (
                    <option value={k} key={k}>
                      {v}
                    </option>
                  ))}
              </select>
              <button
                disabled={disabled}
                onClick={async () => {
                  if (await post(`/api/robots/${robot}/fault`, { kind: fault }))
                    setNotice("已注入故障。点击「开始诊断」运行完整闭环。");
                }}
              >
                注入故障
              </button>
              <button
                className="quiet"
                disabled={disabled}
                onClick={async () => {
                  if (await post(`/api/robots/${robot}/reset`))
                    setNotice("已恢复默认配置与起点。");
                }}
              >
                重置
              </button>
            </div>
          </section>
          <section className="panel command">
            <div className="panel-head">
              <h2>故障诊断实验 · 目标货架 A (2, 0.5)</h2>
              <span className="mini">失败 → 调查 → 修复 → 重放</span>
            </div>
            <label htmlFor="diagnostic-robot">调查对象</label>
            <select id="diagnostic-robot" value={robot} onChange={(e) => setRobot(e.target.value)}>
              {(data?.robots || []).map((item) => <option value={item.id} key={item.id}>{item.id.replace("robot_", "Robot 0")}</option>)}
            </select>
            <label>诊断模式</label>
            <div className="segmented">
              <button
                className={mode === "ai" ? "chosen" : ""}
                onClick={() => setMode("ai")}
              >
                模型 AI
              </button>
              <button
                className={mode === "rules" ? "chosen" : ""}
                onClick={() => setMode("rules")}
              >
                规则基线
              </button>
            </div>
            <label htmlFor="request">你想排查什么？</label>
            <textarea
              id="request"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              maxLength={2000}
            />
            <label className="checkbox">
              <input
                type="checkbox"
                checked={repair}
                onChange={(e) => setRepair(e.target.checked)}
              />{" "}
              在仿真中执行限定修复并回归
            </label>
            <button
              className="primary"
              disabled={
                disabled ||
                !prompt.trim() ||
                (mode === "ai" && !data?.ai_configured)
              }
              onClick={diagnose}
            >
              {busy ? "正在提交…" : "开始诊断"} <span aria-hidden="true">↗</span>
            </button>
            <button
              className="quiet wide"
              disabled={disabled}
              onClick={async () => {
                const d = await post(`/api/robots/${robot}/navigate`);
                if (d)
                  setNotice(
                    d.success
                      ? "移动任务通过位置验收"
                      : `移动失败：${d.message}`,
                  );
              }}
            >
              仅执行目标点任务
            </button>
            <p className="hint">
              AI 选择调查工具；程序验证修复结果。规则模式不调用模型。
            </p>
          </section>
        </div>
        <section id="investigation" className="panel investigation">
          <div className="panel-head">
            <h2>
              调试任务 <small>TRACE & EVIDENCE</small>
            </h2>
            {run && (
              <div className="actions">
                <span className={"status " + run.status}>
                  {labels[run.status] || run.status}
                </span>
                <a href={`/api/runs/${run.id}/export`}>导出 JSON ↗</a>
                {active(run) && (
                  <button disabled={busy} onClick={async () => {
                    const result = await post(`/api/runs/${run.id}/cancel`);
                    if (result) setNotice(result.accepted ? "已请求取消，正在停止任务并释放机器人。" : "该任务已结束，无需取消。");
                  }}>
                    取消任务
                  </button>
                )}
              </div>
            )}
          </div>
          <div className="investigation-body">
            <div className="run-list">
              {data?.runs.length ? (
                data.runs.map((r) => (
                  <button
                    key={r.id}
                    onClick={() => setSelected(r.id)}
                    className={run?.id === r.id ? "current" : ""}
                  >
                    <b>
                      {r.robot_id.replace("robot_", "Robot 0")}{" "}
                      <em>{r.mode === "ai" ? "AI" : "RULE"}</em>
                    </b>
                    <span>{labels[r.status] || r.status}</span>
                    <small>
                      {new Date(r.created * 1000).toLocaleTimeString()} ·{" "}
                      {r.id.slice(0, 6)}
                    </small>
                  </button>
                ))
              ) : (
                <p className="empty-small">还没有调试记录</p>
              )}
            </div>
            <div className="run-detail">
              {!run ? (
                <div className="empty">
                  <div>⌁</div>
                  <h3>从一个可复现的问题开始</h3>
                  <p>
                    选择机器人，注入一种故障，再开始诊断。
                    <br />
                    工具调用、证据和回归结果将在这里逐步出现。
                  </p>
                </div>
              ) : (
                <>
                  <div className="metrics">
                    <div>
                      <small>修复前 · 原始任务</small>
                      <b>
                        {run.baseline
                          ? run.baseline.success
                            ? "通过"
                            : "未到达"
                          : "等待复现"}
                      </b>
                    </div>
                    <div>
                      <small>Gazebo 独立位置误差 · 通过阈值 &lt; 0.35</small>
                      <b>
                        {run.validation
                          ? `${run.validation.error.toFixed(3)}`
                          : "—"}
                      </b>
                    </div>
                    <div>
                      <small>诊断 tokens</small>
                      <b>
                        {run.usage.prompt_tokens + run.usage.completion_tokens}
                      </b>
                    </div>
                    <div>
                      <small>证据与事件</small>
                      <b>{run.events.length}</b>
                    </div>
                  </div>
                  <div className="diagnosis" role="status">
                    <span>本次任务结果 · {run.backend || run.mode}</span>
                    <p>{run.robot_id} → 目标 ({(run.target || [2, 0.5]).join(", ")})</p>
                    <p>{run.status === "verified"
                      ? run.baseline?.success
                        ? "原始任务已到达目标，本次验证通过。"
                        : "修复前未到达 → 已执行修复 → 复测到达目标，验证通过。"
                      : run.status === "diagnosed"
                        ? "已完成诊断，未开启自动修复。"
                        : `当前状态：${labels[run.status] || run.status}，尚未确认任务恢复。`}</p>
                    <small>{run.baseline?.success
                      ? "首次执行已通过位置验收，无需修复。"
                      : "首次执行的失败记录会保留；最终结果请看本栏和时间线中的独立位置验收。"}</small>
                  </div>
                  {run.plan && <div className="diagnosis">
                    <span>Assistant 分派记录</span>
                    <p>{run.plan.message}</p>
                    <small>规划用量独立记录，不计入上方诊断 tokens：{(run.plan.usage?.prompt_tokens || 0) + (run.plan.usage?.completion_tokens || 0)}</small>
                  </div>}
                  {run.diagnosis && (
                    <div className="diagnosis">
                      <span>诊断结论 / {causes[run.diagnosis.cause]}</span>
                      <p>{run.diagnosis.summary}</p>
                      <small>{run.diagnosis.repair_rationale}</small>
                      <div>
                        {run.diagnosis.evidence_ids.map((id) => (
                          <a key={id} href={"#" + run.id + id}>
                            {id}
                          </a>
                        ))}
                      </div>
                    </div>
                  )}
                  {run.error && <p className="error">{run.error}</p>}
                  <div className="timeline">
                    {run.events.map((e) => (
                      <details id={run.id + e.id} key={e.id}>
                        <summary>
                          <span className={"event-dot " + e.kind} />
                          <code>{e.id}</code>
                          <b>{e.title}</b>
                          <time>
                            {new Date(e.time * 1000).toLocaleTimeString()}
                          </time>
                        </summary>
                        <pre>
                          {JSON.stringify(e.data, null, 2) || "事件已记录"}
                        </pre>
                      </details>
                    ))}
                  </div>
                </>
              )}
            </div>
          </div>
        </section>
        <SourceCase />
        <footer>
          ROBOT DEBUG LAB{" "}
          <span>Gazebo 现场 · Nav2 导航 · AI 调查 · 受限修复 · 独立验收</span>
        </footer>
      </main>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
