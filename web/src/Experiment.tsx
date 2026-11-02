import React, { useEffect, useState } from "react";
import type { Metrics } from "./types";
import { strategies } from "./format";

const ORDER = ["rules", "no_tools_llm", "tools_llm"];
const pct = (v: number) => `${Math.round(v * 100)}%`;
const frac = (v: number, n: number) => `${Math.round(v * n)}/${n}`;

export default function Experiment() {
  const [m, setM] = useState<Metrics | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    fetch("/api/metrics")
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then(setM)
      .catch(() => setError(true));
  }, []);
  const rows = (m?.diagnosis_by_strategy || [])
    .slice()
    .sort((a, b) => ORDER.indexOf(a.strategy) - ORDER.indexOf(b.strategy));
  const maxTokens = Math.max(1, ...rows.map((r) => r.average_total_tokens));
  return <section id="experiment" className="block">
    <div className="block-head">
      <div>
        <span className="kicker">实验</span>
        <h2>规则手册之外的故障，谁能查出来？</h2>
      </div>
      <code className="muted small">{m?.sample.experiment_ids.join(", ")}</code>
    </div>
    <p className="lede">
      注入一个从未写进提示词和规则的故障：AMCL 保持 active，但停止发布 <code>map→odom</code>。
      三种策略面对同一现场，诊断后都经过同一个确定性授权器。
    </p>
    {error && <p className="callout bad">指标读取失败。</p>}
    {m && <>
      <table className="results">
        <thead><tr>
          <th>策略</th><th>识别为未知故障</th><th>定位到 TF</th><th>诊断工具</th>
          <th>修复申请 / 授权</th><th>错误修复</th><th>平均耗时</th><th>平均 tokens</th>
        </tr></thead>
        <tbody>{rows.map((r) => <tr key={r.strategy} className={r.strategy}>
          <th>{strategies[r.strategy]}<small>{r.backend === "rules" ? "固定规则" : `${r.backend} · ${r.model}`}</small></th>
          <td className="num">{frac(r.unknown_fault_classification_rate, r.trials)}</td>
          <td className="num">{frac(r.unknown_subsystem_accuracy, r.trials)}</td>
          <td className="num">{r.average_diagnostic_tool_calls.toFixed(1)}</td>
          <td className="num">{r.repair_requests} / {r.repairs_authorized}</td>
          <td className="num">{r.wrong_repairs}</td>
          <td className="num">{r.average_investigation_seconds.toFixed(1)} s</td>
          <td className="bar-cell">
            <span className="bar" style={{ width: pct(r.average_total_tokens / maxTokens) }} />
            <span className="num">{Math.round(r.average_total_tokens).toLocaleString()}</span>
          </td>
        </tr>)}</tbody>
      </table>
      <div className="findings">
        <p><b>读法。</b>规则在已知故障上 0 token、毫秒级完成，但这次两次都判为健康。
          无工具模型只能靠先验猜，一次猜成定位漂移并申请重置 AMCL，被授权器用新鲜证据拒绝。
          工具模型两次都查到 <code>map→odom</code> 持续老化，判为白名单外的 TF 故障并升级人工，代价约为无工具模型的 5 倍 tokens。</p>
        <p><b>边界。</b>
          {m.sample.small_sample_warning && <mark>小样本</mark>}
          每组 {rows[0]?.trials} 次、1 种未见机制，只描述本次受控实验。
          只统计带私有实验标签的 {m.sample.run_count} 次运行，排除了 {m.sample.unlabeled_runs_excluded} 次未标注的演示运行。
          诊断工具集包含 TF 检查，但提示词未提及任何 TF 故障。</p>
      </div>
    </>}
  </section>;
}
