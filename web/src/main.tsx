import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
type Pose = { x: number; y: number; theta: number; speed: number };
type Robot = {
  id: string;
  pose?: Pose;
  trace: number[][];
  controller_alive: boolean;
  busy: boolean;
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
};
type State = {
  robots: Robot[];
  runs: Run[];
  model: string;
  ai_configured: boolean;
  token: string;
  ros_domain: string;
};
const labels: Record<string, string> = {
  queued: "排队",
  reproducing: "复现故障",
  investigating: "调查中",
  repairing: "执行修复",
  validating: "独立验收",
  verified: "验证通过",
  unresolved: "尚未解决",
  failed: "执行失败",
  canceled: "已取消",
  diagnosed: "诊断完成",
  interrupted: "任务中断",
};
const causes: Record<string, string> = {
  controller_stopped: "控制节点停止",
  topic_mismatch: "速度话题接错",
  zero_speed: "速度上限为零",
  healthy: "状态正常",
  unknown: "证据不足",
};
const active = (r: Run) =>
  [
    "queued",
    "reproducing",
    "investigating",
    "repairing",
    "validating",
  ].includes(r.status);
function Scene({ robots }: { robots: Robot[] }) {
  return (
    <svg
      viewBox="0 0 600 430"
      role="img"
      aria-label="两台机器人实时轨迹和目标位置"
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
      <rect x="40" y="25" width="520" height="370" rx="8" fill="url(#grid)" />
      <path d="M40 25 V395 H560" fill="none" stroke="#496067" />
      {[0, 2, 4, 6, 8, 10].map((n) => (
        <g key={n}>
          <text x={40 + n * 47} y="416" fill="#6d8a91" fontSize="11">
            {n}
          </text>
          <text x="15" y={395 - n * 33} fill="#6d8a91" fontSize="11">
            {n}
          </text>
        </g>
      ))}
      <g transform={`translate(${40 + 8 * 47},${395 - 8 * 33})`}>
        <circle r="20" fill="none" stroke="#d3e885" strokeDasharray="4 4" />
        <path d="M-7 0H7M0-7V7" stroke="#d3e885" />
        <text x="26" y="4" fill="#d3e885" fontSize="12">
          目标 (8, 8)
        </text>
      </g>
      {robots.map((r, i) => {
        const color = i ? "#aeb9ff" : "#62e0c1";
        const p = r.pose;
        return (
          <g key={r.id}>
            <polyline
              points={r.trace
                .map(([x, y]) => `${40 + x * 47},${395 - y * 33}`)
                .join(" ")}
              fill="none"
              stroke={color}
              strokeWidth="2"
              opacity=".65"
            />
            {p && (
              <g transform={`translate(${40 + p.x * 47},${395 - p.y * 33})`}>
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
      <text x="52" y="47" fill="#66848d" fontSize="10">
        LIVE ROS 2 TELEMETRY · POSITION
      </text>
    </svg>
  );
}
function App() {
  const [data, setData] = useState<State | null>(null),
    [connected, setConnected] = useState(false),
    [robot, setRobot] = useState("robot_2"),
    [selected, setSelected] = useState(""),
    [mode, setMode] = useState("ai"),
    [fault, setFault] = useState("topic_mismatch"),
    [prompt, setPrompt] = useState(
      "排查为什么无法到达目标点。收集证据，必要时对照另一台机器人，并验证修复效果。",
    ),
    [repair, setRepair] = useState(true),
    [busy, setBusy] = useState(false),
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
  const run = data?.runs.find((r) => r.id === selected) || data?.runs[0];
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
      robots: robot === "all" ? ["robot_1", "robot_2"] : [robot],
      mode,
      prompt,
      auto_repair: repair,
    });
    if (d) setSelected(d.run_ids[0]);
  }
  const chosen = data?.robots.find((r) => r.id === robot);
  const disabled =
    busy ||
    !connected ||
    (robot === "all" ? data?.robots.some((r) => r.busy) : chosen?.busy);
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
          <span>双机器人仿真实验</span>
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
              {data?.model || "模型连接中"}
              <br />
              ROS domain {data?.ros_domain || "—"}
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
        <div className="workspace">
          <section className="panel scene">
            <div className="panel-head">
              <h2>运行现场</h2>
              <span>
                <i className={connected ? "on" : ""} /> LIVE
              </span>
            </div>
            <Scene robots={data?.robots || []} />
            <div className="legend">
              <span>
                <i className="dot mint" /> Robot 01
              </span>
              <span>
                <i className="dot purple" /> Robot 02
              </span>
              <small>轨迹来自真实 TurtleSim /pose 消息</small>
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
                  .slice(0, 3)
                  .map(([k, v]) => (
                    <option value={k} key={k}>
                      {v}
                    </option>
                  ))}
              </select>
              <button
                disabled={disabled || robot === "all"}
                onClick={async () => {
                  if (await post(`/api/robots/${robot}/fault`, { kind: fault }))
                    setNotice("已注入故障。点击「开始诊断」运行完整闭环。");
                }}
              >
                注入故障
              </button>
              <button
                className="quiet"
                disabled={disabled || robot === "all"}
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
              <h2>Assistant 调试入口</h2>
              <span className="mini">01 → 02</span>
            </div>
            <label>调查对象</label>
            <select value={robot} onChange={(e) => setRobot(e.target.value)}>
              <option value="robot_1">Robot 01</option>
              <option value="robot_2">Robot 02</option>
              <option value="all">两台机器人 · 独立上下文并行</option>
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
              {busy ? "正在提交…" : "开始诊断"} <span>↗</span>
            </button>
            <button
              className="quiet wide"
              disabled={disabled || robot === "all"}
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
        {notice && (
          <div role="status" className="notice">
            {notice}
            <button onClick={() => setNotice("")}>×</button>
          </div>
        )}
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
                  <button onClick={() => post(`/api/runs/${run.id}/cancel`)}>
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
                      <small>原始任务</small>
                      <b>
                        {run.baseline
                          ? run.baseline.success
                            ? "通过"
                            : "未到达"
                          : "等待复现"}
                      </b>
                    </div>
                    <div>
                      <small>回归误差 · 仿真单位</small>
                      <b>
                        {run.validation
                          ? `${run.validation.error.toFixed(3)}`
                          : "—"}
                      </b>
                    </div>
                    <div>
                      <small>模型 tokens</small>
                      <b>
                        {run.usage.prompt_tokens + run.usage.completion_tokens}
                      </b>
                    </div>
                    <div>
                      <small>证据与事件</small>
                      <b>{run.events.length}</b>
                    </div>
                  </div>
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
        <footer>
          ROBOT DEBUG LAB{" "}
          <span>仿真中的可验证诊断 · 每台机器人一个 Agent · 独立位置验收</span>
        </footer>
      </main>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
