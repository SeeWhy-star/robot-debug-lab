# OpenFang 接入

项目通过 OpenFang REST API 为每次调查创建独立 Agent 会话。OpenFang 负责模型会话与调用协议，本地网关仍掌握 ROS 工具、机器人锁、修复白名单和最终验收。

## 配置

```bash
bash scripts/install_openfang.sh
```

在 `.env` 中设置：

```dotenv
RDL_AGENT_BACKEND=openfang
OPENFANG_URL=http://127.0.0.1:4200
OPENFANG_API_KEY=
OPENFANG_PROVIDER=default
OPENFANG_MODEL=default
```

`bash scripts/start_all.sh` 会复用健康的本地 OpenFang；若默认地址没有服务，则自动启动项目隔离的 daemon。检查连接可运行：

```bash
source scripts/env.bash
.venv/bin/python scripts/check_openfang.py
```

## 调查协议

Agent 只能请求以下本地只读工具：

- `inspect_graph`：Nav2 节点、雷达速率和位姿；
- `measure_topics`：原始雷达与 Nav2 输入速率；
- `read_parameters`：受控雷达输入参数；
- `sample_motion`：两次位置采样；
- `finish_diagnosis`：提交结构化结论和真实证据编号。

复杂任务规划使用另一组受限工具：语义场景查询、单个或批量地点解析、机器人状态、能力目录和 `finish_plan`。OpenFang 输出的 JSON、OpenAI 风格工具调用和 DeepSeek DSML 都会在网关边界规范化；工具仍需通过白名单、参数、场景证据和任务 Schema 校验。多地点任务优先一次批量解析，重复观测会被拒绝并要求结束规划。

OpenFang manifest 不授予 shell、文件、网络工具、共享记忆或派生 Agent 权限。模型返回无效 JSON、未知工具、伪造证据编号或超过轮次上限时，任务显式失败，不会回退成规则结果。

复杂任务故障矩阵把计划生成和故障调查的 token 分开记录。同一 trial 只生成一次结构化计划，AI/规则和三种故障共用这份计划；因此对照差异来自恢复策略，而不是重新规划。当前三轮结果全部恢复，但 AI 诊断 token 在不同运行间仍有明显波动。网关已避免把无工具调用的超长自由文本回复带入下一轮上下文，并显式记录 warning；还需要用后续重复矩阵验证实际成本分布是否收敛。

## 当前边界

这是应用层 REST 适配，不是 OpenFang 原生 MCP 或跨机器 Agent 集群。一次调查最多 6 轮，每轮最多 5 个工具调用。远端会话结束后删除；删除失败会在任务时间线记录 warning。模型推理不进入 Nav2 的实时控制环。
