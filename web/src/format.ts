import type { Run } from "./types";

export const robotName = (id = "robot_1") => id.replace("robot_", "Robot 0");
export const robotShort = (id = "robot_1") => id.replace("robot_", "R0");
export const robotColor = (id: string) => (id === "robot_2" ? "var(--r2)" : "var(--r1)");

export const phaseLabels: Record<string, string> = {
  planned: "已计划",
  navigating: "导航中",
  waiting_route: "等待路线",
  executing: "执行动作",
  investigating: "调查中",
  repairing: "修复中",
  replaying: "重放原目标",
  verifying: "Gazebo 验收中",
  verified: "验收通过",
  nav2_ok_gazebo_failed: "Nav2 成功 · 真值未通过",
  failed: "失败",
  escalated: "已升级人工",
  canceled: "已取消",
};

// Legacy runs created before contract v2 only carry `status`.
const legacyPhase: Record<string, string> = {
  queued: "planned",
  reproducing: "navigating",
  dispatched: "navigating",
  traffic_waiting: "waiting_route",
  validating: "verifying",
  waiting: "executing",
  simulated_charging: "executing",
  idle: "executing",
  unresolved: "escalated",
  diagnosed: "escalated",
  interrupted: "failed",
};
export const phaseOf = (r: Run) => r.phase || legacyPhase[r.status] || r.status;
export const TERMINAL = ["verified", "nav2_ok_gazebo_failed", "failed", "escalated", "canceled"];
export const isTerminal = (r: Run) => r.terminal ?? TERMINAL.includes(phaseOf(r));
export const phaseTone = (phase: string) =>
  phase === "verified" ? "ok"
    : phase === "escalated" || phase === "nav2_ok_gazebo_failed" ? "warn"
      : phase === "failed" ? "bad"
        : phase === "canceled" ? "muted"
          : "live";

export const causes: Record<string, string> = {
  scan_interrupted: "导航雷达数据中断",
  navigator_inactive: "BT Navigator 生命周期停用",
  controller_velocity_zero: "Controller 速度限制为零",
  localization_drift: "AMCL 定位漂移",
  transform_updates_stopped: "TF map→odom 停止更新",
  healthy: "状态正常",
  unknown: "未知 / 证据不足",
};
export const classifications: Record<string, string> = {
  known_fault: "已知故障",
  unknown_fault: "未知故障",
  insufficient_evidence: "证据不足",
  healthy: "系统健康",
};
export const subsystems: Record<string, string> = {
  sensor_pipeline: "传感器链路",
  tf: "TF 坐标变换",
  localization: "定位",
  nav2_lifecycle: "Nav2 生命周期",
  planner: "全局规划",
  controller: "控制器",
  costmap: "代价地图",
  qos_or_transport: "QoS / 传输",
  simulation: "仿真",
  unknown: "未定位",
};
export const strategies: Record<string, string> = {
  rules: "规则基线",
  no_tools_llm: "无工具模型",
  tools_llm: "工具模型",
};
export const actors: Record<string, string> = {
  diagnostic_agent: "诊断 Agent",
  deterministic_rule: "规则",
  repair_authorizer: "授权器复测",
};
export const operations: Record<string, string> = {
  restore_scan_forwarding: "恢复雷达转发",
  resume_nav2_lifecycle: "恢复 Nav2 生命周期",
  restore_controller_velocity_limits: "恢复控制器速度限制",
  reseed_amcl_from_gazebo_truth: "用 Gazebo 真值重置 AMCL",
};
export const legacyStatus: Record<string, string> = {
  queued: "排队",
  reproducing: "复现故障",
  navigating: "正在导航",
  traffic_waiting: "等待路线通行权",
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
  dispatched: "已分派",
};

export const clock = (t: number) =>
  new Date(t * 1000).toLocaleTimeString("zh-CN", { hour12: false });
export const tokens = (u?: { prompt_tokens: number; completion_tokens: number }) =>
  (u?.prompt_tokens || 0) + (u?.completion_tokens || 0);
