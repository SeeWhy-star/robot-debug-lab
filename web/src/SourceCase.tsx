import React, { useEffect, useState } from "react";

type Summary = {
  status: string; issue: string; commit: string; scope: string;
  diagnosis: string; limitations: string; patch: string; evidence: string; attempts?: number;
  error?: string;
  rows: { name: string; baseline: boolean | null; guard: boolean | null; candidate: boolean | null }[];
};
const names: Record<string, string> = {
  valid_raw: "正常原始地图更新", valid_grid: "正常占据栅格更新", disabled: "禁用层停止发布",
  overflow: "越界更新安全拒绝", empty: "空地图安全拒绝", recovery: "异常后正常更新恢复",
};
const verdict = (value: boolean | null) => value === true ? "通过" : value === false ? "失败" : "未验证";

export default function SourceCase() {
  const [data, setData] = useState<Summary | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(false);
  async function refresh() {
    setLoading(true);
    try {
      const response = await fetch("/api/source-cases/nav2-5710");
      if (!response.ok) throw new Error("unavailable");
      setData(await response.json()); setError(false);
    } catch { setError(true); } finally { setLoading(false); }
  }
  useEffect(() => { void refresh(); }, []);
  return <section id="source-case" className="panel source-case">
    <div className="panel-head"><h2>真实源码案例 <small>NAV2 #5710</small></h2>
      <button onClick={refresh} disabled={loading}>{loading ? "读取中…" : "刷新案例结果"}</button></div>
    <div className="source-case-body">
      {error ? <p role="alert">案例结果读取失败，请稍后刷新。</p> : !data ? <p>正在读取案例…</p> : <>
        <div className="diagnosis">
          <span>{data.status === "component_verified" ? "AI 补丁通过组件验收" : data.status === "not_run" ? "尚未运行案例" : "已复现故障，修复尚未通过验收"}</span>
          <p>真实 Nav2 地图发布器 · Ubuntu 24.04 / ROS 2 Jazzy · C++ 内存检测</p>
          <small>{data.scope}</small>
        </div>
        <p><a href={data.issue} target="_blank" rel="noreferrer">查看上游故障报告 ↗</a> · 源码版本 <code>{data.commit.slice(0, 12)}</code></p>
        {data.rows.length > 0 && <div className="case-table-wrap"><table>
          <thead><tr><th>行为检查</th><th>原始源码</th><th>仅禁用层保护</th><th>AI 候选补丁</th></tr></thead>
          <tbody>{data.rows.map(row => <tr key={row.name}><td>{names[row.name]}</td>
            {[row.baseline, row.guard, row.candidate].map((v, i) => <td key={i} className={v === true ? "case-pass" : v === false ? "case-fail" : ""}>{verdict(v)}</td>)}
          </tr>)}</tbody></table></div>}
        <p className="hint">“仅禁用层保护”是根据上游 PR #5748 思路移植的对照补丁，不代表上游完整版本。失败表示未满足该项行为要求。</p>
        {data.diagnosis && <><h3>AI 源码调查 · {data.attempts} 次候选验证</h3><p>{data.diagnosis}</p><p className="hint">{data.limitations}</p></>}
        {data.error && <p role="alert" className="case-error">模型未产出可验收补丁：{data.error}</p>}
        {data.evidence && <details><summary>查看原始越界证据</summary><pre>{data.evidence}</pre></details>}
        {data.patch && <details><summary>查看 AI 候选补丁</summary><pre>{data.patch}</pre></details>}
        <p className="hint">重新验收：在项目终端运行 <code>.venv/bin/python scripts/nav2_case.py all</code>，结束后刷新本栏。补丁仅应用于隔离案例副本。</p>
      </>}
    </div>
  </section>;
}
