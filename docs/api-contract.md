# Robot Debug Lab API Contract v2

本契约是两天冲刺期间后端与前端之间的稳定边界。它先于实现提交；第二步必须用契约测试固定这里定义的枚举、必填字段和向后兼容行为。

设计目标：

- 前端刷新后只依赖 `/api/state` 即可恢复当前任务、调查进度和活动目标。
- 诊断层允许模型表达未知故障，修复授权层继续由确定性程序控制。
- 规则、无工具 LLM、工具 LLM 使用相同诊断结果结构，便于公平评测。
- 现有 `status`、事件 `id/time/kind/title/data` 和 `/api/state` 字段保持可用。
- 新字段只做追加；未升级的前端可以忽略它们。

### 两天冲刺的实现范围

本文保留长期完整契约，但第二步只要求“单机器人导航失败 → 诊断 → 授权 → 修复或升级 → 原目标重放 → run级验收”产生全部v2规范事件。多步骤任务、车队、接替、交通和空间目标本轮只需在 `/api/state` 中暴露正确 `phase`；它们继续产生legacy事件，前端使用通用卡片兜底。非核心路径的phase允许由legacy `status`和当前步骤在API序列化时兼容映射，不要求重写所有工作流分支。

除特别说明外，时间戳均为 Unix seconds（UTC），ID 均为服务端生成的不透明字符串。所有写接口继续要求 `X-Lab-Token`，所有 API 继续只允许本机访问。

## 1. 版本和兼容原则

`/api/state` 和 `/api/metrics` 顶层增加：

```json
{
  "contract_version": "2.0",
  "server_time": 1793608000.125
}
```

兼容规则：

1. run 的旧 `status` 字段保留，含义与当前版本一致；新前端使用 `phase`。
2. run 现有字段不删除或改名，包括 `baseline`、`diagnosis`、`validation`、`events`、`usage`、`steps` 和 `active_target`。
3. 事件继续包含 `id`、`time`、`kind`、`title`、`data`。v2 结构化字段放在 `data` 中。
4. 数据库中的历史事件原样返回；其 `kind` 可能属于本文末尾列出的 legacy 枚举。
5. v2 新事件使用规范 kind。消费者必须忽略未知 kind，不能因增加事件类型而中断渲染。
6. 新诊断结构同时提供 v1 别名：`cause`、`evidence_ids`、`repair_rationale`。未知故障的 `cause` 固定为 `unknown`。
7. `/api/events` 仍以 SSE 每次发送完整 `/api/state` 快照；未来若改为增量事件，必须另升契约版本。

## 2. Run 结构

每个 run 至少提供：

```json
{
  "id": "a1b2c3d4e5f6",
  "robot_id": "robot_1",
  "task_type": "navigation",
  "mode": "ai",
  "status": "investigating",
  "phase": "investigating",
  "phase_updated_at": 1793608001.250,
  "phase_context": {
    "reason": "initial_navigation_failed",
    "step_id": "S1",
    "activity": "system_diagnosis",
    "return_to": null
  },
  "terminal": false,
  "created": 1793607980.100,
  "finished": null,
  "active_target": [2.0, 0.5],
  "events": [],
  "usage": {"prompt_tokens": 0, "completion_tokens": 0},
  "diagnosis": null,
  "repair_authorization": null,
  "validation": null
}
```

`phase_context` 始终为对象；未知或不适用字段使用 `null`，不要省略。`activity` 用于不值得扩大顶层状态机的细节：

- `mission_step`
- `timed_wait`
- `simulated_charging`
- `enter_idle`
- `system_diagnosis`
- `route_replanning`
- `gazebo_acceptance`

### 2.1 统一 phase 枚举

非终态：

```text
planned
navigating
waiting_route
executing
investigating
repairing
replaying
verifying
```

终态：

```text
verified
nav2_ok_gazebo_failed
failed
escalated
canceled
```

`executing` 是对建议状态机的唯一扩展，用于现有的 `wait / simulate_charge / enter_idle`；这些动作不能伪装成导航。任务级路线 Agent 运行时仍为 `investigating`，并将 `activity` 设为 `route_replanning`。

### 2.2 合法转换

并非每个 run 都经过所有状态。合法主路径如下：

```text
planned
  ├─> navigating
  │     ├─> waiting_route ─> navigating
  │     ├─> executing ─> navigating
  │     ├─> verifying ─> verified
  │     │                └> nav2_ok_gazebo_failed
  │     └─> investigating
  │           ├─> escalated
  │           ├─> failed
  │           ├─> repairing ─> replaying
  │           │                  ├─> waiting_route ─> replaying
  │           │                  ├─> verifying ─> verified
  │           │                  │                └> nav2_ok_gazebo_failed
  │           │                  └─> investigating
  │           └─> navigating  (受限路线重规划后)
  └─> executing
          ├─> navigating
          └─> verifying ─> verified
```

任何非终态均可转换到 `failed` 或 `canceled`。服务器重启中断的旧 run 保持 legacy `status="interrupted"`，v2 `phase="failed"`，`phase_context.reason="server_restarted"`。

多步骤任务可以在 `navigating` 与 `executing` 之间多次往返。每一步开始或结束只更新步骤状态并产生 `step_update`（本轮实现仍可保留legacy步骤事件）；`verifying`只表示整个run的最终验收，不用于每个中间步骤。

### 2.3 每个转换发生的位置

| 转换 | 触发点 | 后端责任位置 |
| --- | --- | --- |
| 创建 → `planned` | run 已持久化、尚未控制机器人 | `Store.new` / `Workflow.submit_*` |
| `planned` → `navigating` | 即将预览路径或发送首次 Nav2 目标 | `Workflow._mission_navigation` / `Workflow.run` |
| `navigating` → `waiting_route` | 已取得 `ComputePathToPose` 结果，正在等待路线预约 | `Workflow._navigate_with_traffic` 调用 `traffic.acquire` 前 |
| `waiting_route` → `navigating` | 初次导航取得通行权 | `traffic.acquire` 成功后、`bridge.navigate` 前 |
| `planned` → `executing` | 开始 wait、仿真充电或 idle | `Workflow.run_mission` 的原子步骤分支 |
| 导航失败 → `investigating` | 首次失败已经记录，尚未请求诊断 | `Workflow._mission_navigation` / `Workflow.run` |
| `investigating` → `repairing` | `repair_authorization.decision="authorized"` 已持久化 | 新的确定性 RepairAuthorizer 返回后、`bridge.repair` 前 |
| `investigating` → `escalated` | 未知故障、证据不足、无白名单或授权拒绝 | 诊断保存和授权判定完成后 |
| `repairing` → `replaying` | 白名单动作成功，准备重放同一目标 | `bridge.repair` 返回后、再次导航前 |
| `replaying` → `waiting_route` | 重放目标等待路线预约 | `_navigate_with_traffic`，`phase_context.return_to="replaying"` |
| Nav2成功 → `verifying` | Nav2 action 已返回成功，尚未接受Gazebo真值 | 导航结果保存后、Gazebo验收前 |
| `verifying` → `verified` | Gazebo新鲜真值通过全部阈值 | 单步或任务最终验收完成后 |
| `verifying` → `nav2_ok_gazebo_failed` | Nav2成功但Gazebo真值缺失、陈旧或超阈值 | 验收失败分支 |
| 任意非终态 → `failed` | 执行异常、路线等待超时或已知故障恢复失败 | 工作流异常/失败出口 |
| 任意非终态 → `canceled` | 用户取消已被执行线程确认 | `Workflow.cancel` 对应的执行线程出口 |

每次转换必须先更新 `phase/phase_updated_at/phase_context/terminal`，再写一条 `phase_changed` 事件；两者在同一 Store 锁内持久化。前端以 run 当前 `phase` 为准，以事件显示历史。

## 3. 结构化事件

### 3.1 通用 envelope

所有 v2 新事件具有：

```json
{
  "id": "E7",
  "time": 1793608002.500,
  "kind": "tool_call",
  "title": "读取 TF 健康状态",
  "data": {},
  "schema_version": 2,
  "run_id": "a1b2c3d4e5f6",
  "robot_id": "robot_1",
  "step_id": "S1"
}
```

`step_id` 可以为 `null`。事件中的 `id` 是 run 内证据ID；诊断引用使用同一个ID。

### 3.2 v2 kind 枚举和固定 data

#### `phase_changed`

```json
{
  "from": "navigating",
  "to": "investigating",
  "reason": "initial_navigation_failed",
  "activity": "system_diagnosis",
  "return_to": null
}
```

#### `plan`

```json
{
  "plan_id": "p1",
  "plan_type": "single|fleet",
  "planner_backend": "openfang|direct|deterministic",
  "step_count": 2,
  "summary": "结构化计划摘要"
}
```

#### `navigation_attempt`

```json
{
  "attempt": 1,
  "stage": "initial|post_repair_replay|secondary_repair_replay|replanned",
  "target": [2.0, 0.5],
  "nav2_success": false,
  "nav2_status": "timeout",
  "duration_seconds": 30.0,
  "final_pose": null
}
```

#### `route_wait`

```json
{
  "state": "queued|granted|timed_out|released",
  "reservation_token": null,
  "blocked_by": ["robot_2"],
  "wait_seconds": 1.25,
  "timeout_seconds": 45.0,
  "path_mode": "nav2_geometry|global_fallback"
}
```

#### `agent_decision`

```json
{
  "agent": "diagnostic",
  "mode": "no_tools_llm|tools_llm",
  "round": 2,
  "requested_tools": ["inspect_tf"],
  "reason_summary": "需要确认 map 到 odom 的变换是否连续",
  "usage_delta": {"prompt_tokens": 1200, "completion_tokens": 80}
}
```

`reason_summary` 是简短可展示摘要，不保存隐藏推理过程。

#### `tool_call`

```json
{
  "tool": "inspect_tf",
  "actor": "diagnostic_agent",
  "input_summary": {"frames": ["map", "odom", "base_link"]},
  "output_summary": {"available": false, "missing_edge": "map->odom"},
  "evidence_id": "E7",
  "duration_ms": 41,
  "success": true,
  "error": null
}
```

`actor` 枚举为 `diagnostic_agent | deterministic_rule | repair_authorizer | deterministic_preflight`。诊断Agent只能引用在其诊断结束前已经存在的证据；不能引用随后由 `repair_authorizer` 产生的证据。

工具输出必须经过后端压缩和脱敏；不得把任意日志、模型密钥或故障注入标签放入事件。

#### `diagnosis`

`data` 必须与第4节诊断结构完全相同。

#### `repair_authorization`

`data` 必须与第5节授权结构完全相同。

#### `repair_execution`

```json
{
  "operation": "resume_nav2_lifecycle",
  "known_cause": "navigator_inactive",
  "started_at": 1793608003.0,
  "finished_at": 1793608004.5,
  "success": true,
  "result_summary": {"bt_navigator": "active"},
  "error": null
}
```

#### `verification`

```json
{
  "scope": "navigation|mission|fleet",
  "nav2_success": true,
  "gazebo_success": true,
  "pose_source": "gazebo_odometry",
  "position_error_m": 0.12,
  "threshold_m": 0.35,
  "observed_at": 1793608006.0,
  "reason": null
}
```

#### `escalation`

```json
{
  "reason": "unknown_fault|insufficient_evidence|no_allowlisted_repair|authorization_denied",
  "suspected_subsystem": "tf",
  "summary": "map 到 odom 的变换不可用，需要人工检查定位发布链路",
  "cited_evidence": ["E3", "E7"],
  "recommended_action": "检查 robot_1 的 map->odom TF 发布者",
  "repair_executed": false
}
```

#### `step_update`

```json
{
  "action": "navigate",
  "state": "started|completed|failed|canceled",
  "description": "前往货架A",
  "failure_reason": null
}
```

#### `warning`

```json
{"code": "model_protocol_retry", "message": "模型工具格式无效，已要求重试"}
```

#### `error`

```json
{"code": "workflow_error", "message": "面向用户的有限错误摘要", "retryable": false}
```

#### `cancellation`

```json
{"requested_by": "user", "confirmed": true, "reason": "用户取消任务"}
```

### 3.3 Legacy kind

历史数据可以继续出现：

```text
assistant, plan, model, tool, experiment, traffic_path, traffic_reserved,
traffic_waited, traffic_timeout, traffic_released, diagnosis, guard, repair,
validation, route_preflight, failure_transition, replan_started, replan_applied,
step_started, step_completed, step_failed, charging_started,
charging_completed, idle, mission_validation, fleet_spatial_validation,
handoff_skipped, handoff_dispatched, handoff_validation, warning, error, cancel
```

第二步只要求新路径产生规范事件；不迁移SQLite中的历史记录。`/api/state` 返回历史事件时保持原样。

## 4. 新诊断输出结构

三种实验模式都返回相同结构：

```json
{
  "schema_version": 2,
  "strategy": "rules|no_tools_llm|tools_llm",
  "classification": "known_fault|unknown_fault|insufficient_evidence|healthy",
  "known_cause": null,
  "suspected_subsystem": "tf",
  "summary": "map 到 odom 的变换不可用，现有白名单没有对应修复",
  "cited_evidence": ["E3", "E7"],
  "ruled_out": [
    {
      "hypothesis": "scan_interrupted",
      "reason": "原始和Nav2雷达输入频率均正常",
      "evidence_ids": ["E5"]
    }
  ],
  "repair_requested": false,
  "requested_repair": null,
  "recommended_action": "升级人工检查 TF 发布节点",
  "cause": "unknown",
  "evidence_ids": ["E3", "E7"],
  "repair_rationale": "未知故障不申请自动修复"
}
```

枚举：

- `classification`: `known_fault | unknown_fault | insufficient_evidence | healthy`
- `strategy`: `rules | no_tools_llm | tools_llm`
- `suspected_subsystem`: `sensor_pipeline | tf | localization | nav2_lifecycle | planner | controller | costmap | qos_or_transport | simulation | unknown`
- `known_cause`: `scan_interrupted | navigator_inactive | controller_velocity_zero | localization_drift | null`
- `requested_repair`: `restore_scan_forwarding | resume_nav2_lifecycle | restore_controller_velocity_limits | reseed_amcl_from_gazebo_truth | null`

约束：

1. `known_fault` 必须有 `known_cause`，其余 classification 的 `known_cause` 必须为 `null`。
2. `repair_requested=true` 只允许用于 `known_fault`，并且 `requested_repair` 必须非空。
3. `unknown_fault` 和 `insufficient_evidence` 必须 `repair_requested=false`。
4. `cited_evidence` 和 `ruled_out[].evidence_ids` 只允许引用当前run、当前robot、诊断结束前已存在的 `tool_call` 或 `navigation_attempt`。
5. `tools_llm` 可以引用上述两类事件；`rules`只能引用 `actor="deterministic_rule"` 的 `tool_call` 和 `navigation_attempt`。
6. `no_tools_llm` 只能引用开始时提供的 `navigation_attempt`，不能引用任何 `tool_call`。引用 `phase_changed`、`warning`、其他run或其他机器人事件必须被拒绝。
7. `ruled_out` 可以为空；每项引用遵守相同类型和作用域限制。
8. `summary/reason/recommended_action` 是面向用户的有限文本，不保存隐藏推理过程。
9. 模型输出诊断后不再要求 `known_cause == infer()`；诊断层只做Schema、证据类型与作用域、时序和文本长度校验。

### 4.1 `no_tools_llm` 的固定输入

无工具组只接收以下完整payload，不得追加现场状态：

```json
{
  "robot_id": "robot_1",
  "task_description": "导航到目标点",
  "target": [2.0, 0.5],
  "navigation_attempt": {
    "evidence_id": "E3",
    "nav2_success": false,
    "nav2_status": "timeout",
    "duration_seconds": 30.0,
    "final_pose": [-2.0, -0.5]
  }
}
```

它可以使用与工具组相同的系统结构说明和正常不变量，但不能获得 `inspect_graph`、节点列表、Topic频率、lifecycle、controller参数、TF、定位偏差、`infer()`结果、故障标签或其他策略收集的证据。该payload字段集合由契约测试精确固定。

## 5. 确定性修复授权

诊断不直接触发修复。服务器收到诊断后重新读取当前机器人状态，生成独立授权记录：

```json
{
  "schema_version": 2,
  "decision": "authorized|denied|not_applicable",
  "known_cause": "navigator_inactive",
  "requested_operation": "resume_nav2_lifecycle",
  "authorized_operation": "resume_nav2_lifecycle",
  "rule_version": "repair-allowlist-v1",
  "checked_at": 1793608002.9,
  "max_evidence_age_seconds": 2.0,
  "evidence_ids": ["E9"],
  "checks": [
    {
      "name": "bt_navigator_not_active",
      "passed": true,
      "expected": "state != active",
      "actual": "inactive"
    }
  ],
  "reason": "新鲜服务器侧复测支持对应白名单修复"
}
```

授权规则：

1. `classification` 必须为 `known_fault`。
2. `repair_requested` 必须为 `true`。
3. `known_cause` 与 `requested_repair` 必须是白名单中的固定映射。
4. 授权器必须在诊断完成后重新调用服务器持有的只读测量，不能只信模型引用的旧证据。
5. 授权测量必须属于同一 `robot_id`，并在授权时不超过2秒；耗时调用完成后以各测量自己的 `observed` 时间判断。
6. 新鲜测量必须满足该白名单动作的确定性前置条件。
7. 未知故障、证据不足、健康状态、无白名单动作或任一检查失败，一律不执行修复。
8. AI模式授权失败时不得降级调用规则修复；run进入 `escalated`，保存拒绝原因。
9. 授权只允许一次具体操作，不授予通用参数写入、shell或速度控制权限。
10. 修复完成只证明动作执行成功；必须重放原目标并经过Gazebo独立验收，才能进入 `verified`。

白名单前置条件保持在后端，不放进诊断提示词：

| known_cause | 唯一允许操作 | 新鲜授权证据 |
| --- | --- | --- |
| `scan_interrupted` | `restore_scan_forwarding` | 原始扫描有数据且Nav2扫描输入为零，故障边界仍是受控转发器 |
| `navigator_inactive` | `resume_nav2_lifecycle` | `bt_navigator` 当前存在且不为active |
| `controller_velocity_zero` | `restore_controller_velocity_limits` | FollowPath前后向速度限制当前同时为零 |
| `localization_drift` | `reseed_amcl_from_gazebo_truth` | AMCL/Gazebo实测偏差达到阈值；仅仿真允许使用Gazebo真值重播种 |

第一批未见TF故障没有白名单修复。无论规则或LLM如何描述，只能产生 `decision="not_applicable"` 或 `denied`，随后 `escalated`。

## 6. `/api/state`

`GET /api/state` 是页面刷新恢复的唯一必需读取接口。顶层结构：

```json
{
  "contract_version": "2.0",
  "server_time": 1793608000.125,
  "robots": [],
  "runs": [],
  "active_run_ids": ["a1b2c3d4e5f6"],
  "active_targets": {"robot_1": [2.0, 0.5]},
  "traffic": {},
  "plans": [],
  "agents": [],
  "model": "configured-model-name",
  "ai_configured": true,
  "token": "local-write-token",
  "ros_domain": "42",
  "runtime": "openfang",
  "simulation": "nav2_gazebo"
}
```

### 6.1 Robot 必填字段

```json
{
  "id": "robot_1",
  "pose": {
    "x": -2.0,
    "y": -0.5,
    "theta": 0.0,
    "speed": 0.0,
    "observed": 1793608000.0,
    "source": "gazebo_odometry"
  },
  "trace": [],
  "controller_alive": true,
  "nav2_online": true,
  "busy": true,
  "scan": {
    "raw_scan_hz": 5.0,
    "nav2_scan_hz": 5.0,
    "scan_forwarding": true,
    "observed": 1793608000.0
  },
  "localization": {
    "status": "aligned|drifted|unavailable",
    "position_error": 0.04,
    "threshold": 0.45,
    "gazebo_pose": {},
    "amcl_pose": {},
    "observed": 1793608000.0
  },
  "runtime": "nav2_gazebo",
  "scenario_active": false
}
```

### 6.2 Run 列表要求

- 最多返回最近100条，按 `created` 降序，与当前行为一致。
- 每条包含第2节字段和完整 `events`，足以重建调查时间线。
- `active_run_ids` 包含所有 `terminal=false` 的run。
- `active_target` 仅在机器人当前确实执行导航或重放时非空；终态、调查、升级和历史run不得成为活动目标。
- `finished` 在非终态为 `null`，终态必须为时间戳。
- `terminal` 必须由 `phase` 计算，不能由前端猜测legacy `status`。
- `repair_authorization` 保存最近一次授权结果；完整授权历史仍在events中。

### 6.3 Traffic 必填字段

保留现有 `active/queued/clearance`，并将queued改为可选明细数组之外仍保留计数：

```json
{
  "active": [],
  "queued": 1,
  "queued_items": [
    {"robot_id": "robot_2", "run_id": "r2", "step_id": "S1", "blocked_by": ["robot_1"]}
  ],
  "clearance": 0.65
}
```

`queued_items` 是新增字段；旧前端继续读取数字 `queued`。

## 7. `/api/metrics`

实验真值不属于run。Store单独创建私有表：

```sql
CREATE TABLE experiment_labels(
    run_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    true_cause TEXT NOT NULL,
    true_subsystem TEXT NOT NULL,
    created REAL NOT NULL
)
```

`Store.get`、`Store.all`、`/api/state`、`/api/runs/{id}`和run导出永远不读取或联结该表。Agent、调查工具和修复授权器没有读取权限。只有实验脚本写入标签，只有metrics聚合器读取标签。

新增只读 `GET /api/metrics`。它只通过 `experiment_labels JOIN runs` 统计带标签的终态run，绝不把普通手动演示混入准确率。查询参数在第二步仅需要支持：

- `limit`: `1..100`，默认100
- `strategy`: 可重复参数，值为 `rules | no_tools_llm | tools_llm`
- `experiment_id`: 可选精确过滤；省略时统计最近100个有标签的实验run，并在响应中列出实际包含的实验ID

响应：

```json
{
  "contract_version": "2.0",
  "server_time": 1793608000.125,
  "sample": {
    "run_count": 12,
    "experiment_ids": ["tf-ablation-20261103"],
    "strategies": ["rules", "no_tools_llm", "tools_llm"],
    "unlabeled_runs_excluded": 17,
    "small_sample_warning": true,
    "notes": "每组样本量较少，结果仅描述本次受控实验"
  },
  "overall": {
    "verified": 5,
    "nav2_ok_gazebo_failed": 1,
    "failed": 1,
    "escalated": 5,
    "canceled": 0,
    "task_completion_rate": 0.417,
    "wrong_repairs": 0
  },
  "diagnosis_by_strategy": [
    {
      "strategy": "tools_llm",
      "model": "configured-model-name",
      "backend": "openfang",
      "prompt_version": "diagnostic-structure-v1-frozen",
      "trials": 4,
      "known_fault_accuracy": 1.0,
      "unknown_fault_classification_rate": 0.75,
      "unknown_subsystem_accuracy": 0.5,
      "insufficient_evidence_rate": 0.0,
      "escalation_rate": 0.5,
      "repair_requests": 2,
      "repairs_authorized": 2,
      "repairs_denied": 0,
      "wrong_repairs": 0,
      "average_tool_calls": 3.25,
      "average_investigation_seconds": 8.4,
      "average_total_tokens": 3200.0
    }
  ],
  "navigation": {
    "attempts": 12,
    "nav2_success_rate": 0.5,
    "gazebo_verification_rate": 0.417,
    "average_final_position_error_m": 0.18
  },
  "traffic": {
    "reservations": 6,
    "conflict_waits": 1,
    "timeouts": 0,
    "total_wait_seconds": 2.1
  }
}
```

指标定义：

- `known_fault_accuracy`: 有隐藏实验真值且属于已知白名单故障的run中，`known_cause`正确的比例。
- `unknown_fault_classification_rate`: 隐藏实验真值属于未见故障时，`classification="unknown_fault"`的比例。
- `unknown_subsystem_accuracy`: 隐藏实验真值属于未见故障时，`suspected_subsystem`与标签表一致的比例。
- `wrong_repairs`: 实际执行的修复与隐藏实验真值不匹配的次数；未执行修复不计错修。
- `task_completion_rate`: `phase="verified"` 的终态run占比。
- `gazebo_verification_rate`: 导航任务中通过Gazebo真值验收的比例。
- `average_total_tokens`: `prompt_tokens + completion_tokens` 的组内均值；规则模式应为0。

隐藏故障真值只供评测器聚合，绝不进入Agent提示词、工具输出、run公共字段或事件。前端只读取聚合指标。结果JSON与metrics分组都必须记录 `model`、`backend`、`prompt_version`和真实样本量。

### 7.1 提示词冻结和实验顺序

诊断提示词只描述系统结构、可用工具和正常不变量，不能出现TF断开或任何未见故障的特征、判定方法、名称暗示或修复建议。执行顺序固定为：

1. 完成提示词改写和相关单元测试。
2. 单独提交并以版本 `diagnostic-structure-v1-frozen` 标记冻结。
3. 冻结后才实现/启用TF故障注入和未见故障评测。
4. 评测开始后不得为提高结果修改该提示词；若必须修复协议错误，升级prompt版本并废弃此前样本，不能混合统计。
5. TF故障进入诊断实现前必须手动复现3次并保存每次Nav2失败表现。表现不稳定时停止，不把它强行加入消融。

## 8. 前端实现约定

Claude前端应：

1. 用 `phase` 驱动主状态，不从中文 `title` 或旧 `status` 猜状态。
2. 用规范事件构建时间线；legacy事件采用通用回退卡片。
3. 把 `diagnosis` 与 `repair_authorization` 分成两张卡，不能把模型建议显示成已授权修复。
4. `escalated` 明确显示“未执行自动修复，等待人工处理”。
5. `nav2_ok_gazebo_failed` 明确区分Nav2 action成功与物理真值验收失败。
6. 页面刷新后先请求 `/api/state`，再订阅 `/api/events`；SSE断开时回退到轮询 `/api/state`。
7. 指标页面始终显示每组 `trials` 和 `small_sample_warning`，不隐藏不利结果。

## 9. 第二步验收条件

第二步只规范化单机器人导航故障核心路径。多步骤、车队、接替、交通和空间路径仅要求 `/api/state` 的phase兼容映射正确，事件保留legacy。`tests/test_contracts.py` 至少覆盖：

- 所有run均含合法 `phase/terminal/phase_context`。
- 合法和非法phase转换。
- 核心路径每个规范事件kind的必填data字段。
- 三种诊断strategy共享同一Schema。
- 未知故障不能请求或获得修复授权。
- 已知故障的错误修复映射被拒绝。
- 授权使用诊断后的新鲜服务器侧证据。
- 引用 `phase_changed`、其他run或其他机器人事件会被拒绝。
- `no_tools_llm` 输入字段精确等于第4.1节，且只能引用 `navigation_attempt`。
- `/api/state` 刷新可以恢复活动run和调查时间线。
- `/api/metrics` 只联结私有标签表，支持 `experiment_id`，按strategy分组并保留模型、backend、prompt版本和真实样本量。
- 普通手动run不会进入诊断准确率。
- 旧 `status`、旧事件envelope和现有 `/api/state` 字段仍然存在。

