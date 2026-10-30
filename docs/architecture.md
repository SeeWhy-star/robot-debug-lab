# 架构与边界

## 一条主链路

系统只运行 Gazebo + Nav2，不再维护 TurtleSim 控制器和自定义导航 Action。

1. FastAPI 启动仓库内置的 Gazebo 世界、TurtleBot3 和 Nav2；Nav2 加载与世界碰撞体对应的占据栅格。
2. 网关让 AMCL 保持读取原始 `/scan`，把障碍物层和碰撞监控改到 `/scan_faulted`，并受控转发 Gazebo 的雷达数据。
3. 导航目标失败后，Agent 只能读取节点图、雷达速率、lifecycle 状态、参数和运动采样。
4. 模型必须通过 `finish_diagnosis` 返回固定结构，并引用本次任务真实存在的事件编号。
5. 网关重新读取现场；只有新鲜证据仍支持同一原因时，才执行匹配的白名单恢复：恢复雷达转发、恢复 lifecycle manager，或恢复 controller 速度参数。
6. 系统重放完全相同的 Nav2 目标，用 Gazebo `/odom` 转换到地图坐标后独立验收。

网页“运行现场”是 Nav2 实际占据栅格上的 2D 遥测视图。Gazebo 原生窗口负责 3D 渲染，RViz 负责展示地图、代价地图、激光和路径。三者共享同一套世界坐标，网页不另外发明货架位置。

## AI 与确定性程序的分工

只有完整匹配一个已知地点或明确坐标的原子指令才由确定性解析器处理。解析器无法完整消费的请求全部进入模型，不使用“然后、回来后、检查”等连接词列表判断复杂度。复杂任务中，AI 通过 `list_scene_objects`、`resolve_destination(s)`、`get_object_pose`、`get_safe_approach_pose`、`get_robot_state` 和 `list_robot_capabilities` 查询现场并输出最多 8 个结构化步骤。多地点任务优先使用一次批量解析；网关拒绝完全相同的重复观测。程序把对象 ID 绑定到注册表中的安全接近点，逐步执行 `navigate / wait / return_to_start / enter_idle / simulate_charge`。

机器人能力目录是有限的，用户表达不需要穷举。模型识别到取货、存货或其他当前无法执行的意图时，必须返回零步骤、说明能力缺口并提出只由现有能力组成的替代方案。补充回复会携带完整澄清历史重新进入 Agent。模型输出经过 OpenFang 协议适配、工具白名单、Pydantic Schema 和跨步骤业务约束校验；例如 `simulate_charge` 必须紧跟在到达 `charging_station` 之后。协议包装、字段或顺序错误会反馈给同一 Agent 自修正，不能降级为规则计划。

AI 还负责开放式故障调查：决定先看节点、参数、雷达速率还是运动状态，并把多条证据组织成原因。程序负责边界：目标范围、机器人锁、工具作用域、调用次数、证据 ID、修复白名单和最终位置阈值。

规则基线执行同一工作流，用三组互不相同的固定条件判断雷达链路、lifecycle 和 controller 配置故障。它用于控制变量，不冒充 AI 结果。

## 任务级重规划

语义对象可以登记多个经过审核的安全接近点。Gazebo 实体障碍导致一次真实导航失败后，程序先读取雷达、lifecycle 和 controller 参数：匹配已知系统故障时进入原诊断/白名单修复链；三者均健康时才进入路线恢复。重规划 Agent 只能调用 `get_mission_state` 和 `list_approach_options`，后者使用 Nav2 `ComputePathToPose` 检查候选点。Agent 只能选择工具返回的可达 `approach_id` 或中止，不能提供坐标、改变目的地、重新执行已完成步骤或删除后续步骤。每次修订都增加计划版本并保留原目标、失败尝试、证据和替代目标。

## 运行状态

诊断任务依次经过 `reproducing → investigating → repairing → validating → verified/unresolved`。多步骤任务为每一步记录开始、完成、失败原因与取消状态，并在最终验收后进入 `verified`。网页只显示后端明确标记的活动目标；拒绝、澄清和历史计划不会继续显示成当前目标。

## 安全边界

Agent 没有 shell、文件写入、故障注入和任意 ROS 参数工具。模型提出的原因不会直接触发动作；程序会用新鲜现场数据再次判断。网页只监听回环地址并使用会话令牌限制写操作。

项目当前是一台仿真机器人、三种可控 Nav2/ROS 故障和单机 OpenFang 会话。实体机器人需要另行加入硬件急停、速度/区域约束、设备身份和人工审批。
