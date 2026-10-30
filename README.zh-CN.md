# Robot Debug Lab

[English](README.md) | 简体中文

一个基于 **Gazebo Sim + Nav2 + OpenFang** 的 ROS 2 故障诊断与恢复工作台。机器人在真实导航栈中执行任务；AI 负责选择调查工具、引用现场证据和提出修复原因，程序只允许执行经过新鲜证据核验的白名单动作，最后用 Gazebo 里程计独立验收原任务。

## 核心案例

仓库机器人从起点前往货架 A `(2.0, 0.5)`。仓库墙体、5 组货架、货架 A、充电站和障碍箱都存在于 Gazebo 世界文件中；后端语义注册表记录实体中心和经过审核的安全接近点，Nav2 不会把障碍物中心当成目标。只有整句能够被完整识别为一个已知地点或明确坐标时，才走 0 token 的确定性路径；未知、复合或带额外动作的表达交给模型查询场景和机器人能力，不能再把整句话误当成地点名称。

例如“机器人去实体障碍箱回来后去充电”会由 OpenFang 一次性核验多个地点，生成并执行“障碍箱安全接近点 → 返回起点 → 充电站 → 明确标注的仿真充电”。网关还会检查步骤间业务约束：仿真充电必须紧跟在到达充电站之后，漏掉导航或把充电放在其他地点的计划会退回同一 Agent 修正，不会下发。“去货架 A 拿货”则会查询能力目录；当前 TurtleBot3 没有抓取技能，因此返回零执行步骤和可用替代方案，不会把拿货偷偷替换成等待或声称完成。用户接受替代方案后，Agent 会结合前后两轮对话重新规划。

故障实验包含三种不同机制：`/scan → /scan_faulted` 中断、Nav2 lifecycle 整组 inactive、controller 前后向速度上限同时为零。AI 看不到注入标签，只能调用只读工具检查 ROS 节点、话题速率、lifecycle 状态、参数和运动状态。网关要求原因与现场证据一致后，才执行对应的白名单修复并重放相同目标。成功与否由 Gazebo 里程计误差 `< 0.35 m` 判定。

本机 2026-10-29 的真实验收结果（摘要见 [验收记录](docs/nav2-acceptance-results.json)）：

| 模式 | 结果 | 修复前误差 | 修复后误差 | 事件 | 模型 tokens |
| --- | --- | ---: | ---: | ---: | ---: |
| 规则基线 | verified | 3.9545 m | 0.1816 m | 7 | 0 |
| OpenFang AI | verified | 3.8998 m | 0.2536 m | 11 | 12132 |

规则模式用于证明故障装置和修复链路本身可靠；AI 模式用于证明模型确实参与调查。模型不直接发布速度，也不能执行 shell、任意参数修改或任意代码。

三种故障的 AI 与规则矩阵均已通过，包含识别、任务恢复、错修复、工具调用、耗时和 token 指标，见 [故障评测结果](docs/fault-evaluation-results.json)。

任务级恢复另有一个真实动态场景：后端通过 Gazebo `set_pose` 把实体 `temporary_crate` 移到货架 A 的默认接近点。首次 Nav2 导航必须实际失败；程序先用新鲜 ROS 状态排除已知系统故障，然后重规划 Agent 读取任务进度和经过审核的候选接近点，只能选择 Nav2 实测可达的替代点。网关保留已完成和后续步骤、记录计划版本，再由 Gazebo 验收重试结果。Agent 看不到场景注入标签，也不能编造坐标。真实结果见[重规划验收记录](docs/replanning-acceptance-results.json)。

## 系统结构

```mermaid
flowchart LR
  UI[React 调试台] --> API[FastAPI 任务网关]
  API --> Nav2[Nav2 导航任务]
  Nav2 -->|失败| Agent[OpenFang 诊断 Agent]
  Agent --> Tools[只读 ROS 工具]
  Tools --> Evidence[带 E 编号的现场证据]
  Evidence --> Guard[新鲜证据核验]
  Guard --> Repair[匹配的白名单恢复]
  Repair --> Replay[重放同一目标]
  Replay --> Odom[Gazebo 里程计验收]
  API --> DB[(SQLite 事件历史)]
```

网页有四个清晰区域：

1. **自然语言任务**：简单目标走确定性语义表；复杂指令通过真实场景工具形成有序步骤；历史下拉框只用于查看记录。
2. **运行现场与故障实验**：显示 Gazebo 里程计轨迹、当前活动目标和雷达速率，支持三种机制不同的故障。
3. **AI 诊断与证据**：对比 AI 和规则基线，查看每次工具调用、证据引用、修复和复测结果。
4. **真实源码案例**：隔离复现 Nav2 #5710，要求 AI 生成受限 C++ 补丁、重新编译并通过独立测试。

## 安装与启动

已验证环境：Ubuntu 26.04 amd64、ROS 2 Lyrical、系统 Python 3.14、Node.js/npm、uv。

```bash
uv venv --system-site-packages --python /usr/bin/python3 .venv
uv pip install --python .venv/bin/python -r requirements.lock
npm --prefix web ci
npm --prefix web run build
[ -f .env ] || cp .env.example .env
chmod 600 .env

# 一次性安装 Nav2、Gazebo Sim 和 TurtleBot3 依赖
bash scripts/install_nav2_sim.sh
bash scripts/check_nav2_sim.sh

# 启动网页、Gazebo/Nav2 和 OpenFang（若 .env 选择 openfang）
bash scripts/start_all.sh
```

打开 <http://127.0.0.1:8765>。网页“运行现场”显示的是 **Nav2 实际占据栅格的 2D 导航视图**，不是 3D 渲染图。`Ctrl+C` 会回收本次启动的服务和仿真进程。使用 `RDL_NATIVE_GUI=1 bash scripts/start_all.sh` 可同时查看 Gazebo 原生 3D 世界和 RViz 导航数据。

场景真值文件是 [`simulation/worlds/rdl_warehouse.sdf.xacro`](simulation/worlds/rdl_warehouse.sdf.xacro)，Nav2 地图是 [`simulation/maps/rdl_warehouse.yaml`](simulation/maps/rdl_warehouse.yaml)。两者必须同步；地图预览由 `simulation/generate_warehouse_map.py` 生成。

AI 模式需要在 `.env` 配置直连模型，或设置 `RDL_AGENT_BACKEND=openfang`。OpenFang 首次安装见 [接入说明](docs/openfang.md)。密钥只放本地 `.env`，不要提交。

## 推荐体验流程

1. 点击“重置”，等待 Robot 01 在线。
2. 输入“去充电站”，确认走确定性语义表且模型 tokens 为 0。
3. 输入“机器人去实体障碍箱回来后去充电”，查看 Agent 场景工具和有序步骤，再观察 Gazebo/Nav2 真实执行。
4. 输入“去货架 A 拿货，然后去充电”，确认系统只提出替代方案，没有分派机器人任务；可继续回复“按第一个替代方案，只去货架 A”。
5. 点击“注入故障”，确认原始雷达仍有数据，而 Nav2 雷达输入变为 0 Hz。
6. 选择“模型 AI”，勾选限定修复，点击“开始诊断”，在时间线查看证据、修复和同目标复测。
7. 切换“规则基线”重复实验，对比事件数、结论和 token 消耗。

自动验收命令会重置仿真并执行完整故障闭环：

```bash
.venv/bin/python scripts/acceptance_nav2.py --mode rules
.venv/bin/python scripts/acceptance_nav2.py --mode ai
.venv/bin/python scripts/acceptance_missions.py --reset
.venv/bin/python scripts/acceptance_replanning.py
.venv/bin/python scripts/evaluate_fault_matrix.py --mode both
```

## 真实 Nav2 源码案例

源码案例固定上游 Navigation2 提交，使用 ASan/UBSan 复现 `nav2_costmap_2d` 越界问题。AI 只能对审核过的 C++ 翻译单元做精确文本替换；每个候选都会重新编译并运行 6 个独立场景。详情与命令见 [英文 README](README.md#real-nav2-source-case) 和网页的“真实源码案例”。

## 验证

```bash
source scripts/env.bash
.venv/bin/python -m pytest -q
npm --prefix web run build
npm --prefix web audit
```

`.runtime/` 保存仿真日志、SQLite 历史和源码案例产物，不会提交。项目是单机仿真研究原型，不代表实体机器人安全系统、生产级调度集群或通用自动修复能力。

更多内容：[架构与边界](docs/architecture.md)、[项目表达](docs/project-brief.md)、[OpenFang](docs/openfang.md)。
