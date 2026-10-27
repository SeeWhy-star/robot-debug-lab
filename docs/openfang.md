# OpenFang 接入与当前边界

本版本提供可选 OpenFang REST 诊断适配器。未配置时默认 direct；使用 openfang 后端的真实闭环验收已通过。
每台机器人在本地拥有独立且绑定 robot_id 的 RobotAgent 实例；每次 OpenFang 调查创建独立远端 Agent，调查结束后删除，避免旧任务证据混入新任务。
自然语言 Supervisor 支持有限澄清续接：解析每机器人一个目标，Workflow 校验并执行计划。规划与机器人诊断使用独立 OpenFang 会话，详见 [Assistant 使用说明](assistant.md)。

## 使用

1. Linux x86_64 可执行 `bash scripts/install_openfang.sh`，安装固定版本 0.6.9 并校验官方 SHA256。运行 `bash scripts/start_openfang.sh` 可单独启动 daemon，也可使用下方的一键启动命令。启动器复用本地 .env 的 LLM_API_KEY、LLM_MODEL、LLM_BASE_URL，密钥仅通过进程环境传递；默认 DeepSeek，其他供应商用 RDL_OPENFANG_PROVIDER 指定。配置和数据仅存 .runtime/openfang，不修改 ~/.openfang。也可使用自行管理的 daemon，参考 [官方入门文档](https://www.openfang.sh/docs/getting-started)。
2. 在项目现有 `.env` 中添加或修改（不要覆盖其他凭据）：

```dotenv
RDL_AGENT_BACKEND=openfang
OPENFANG_URL=http://127.0.0.1:4200
OPENFANG_API_KEY=
OPENFANG_PROVIDER=default
OPENFANG_MODEL=default
```

若自行管理的 daemon 启用了 API 认证，填写对应的 OPENFANG_API_KEY；default 使用 OpenFang 默认模型，也可填写其支持的具体 provider/model。项目启动器只绑定本机 127.0.0.1:4200，不设置 API 认证，OPENFANG_API_KEY 留空；不适合多用户主机或公网部署。

先执行只读连接检查（不会调用模型）：

```bash
source scripts/env.bash
.venv/bin/python scripts/check_openfang.py
```

3. `npm --prefix web run build`，然后停止旧项目服务并运行 `bash scripts/start_all.sh`。该命令自动启动本机 OpenFang，或复用已运行的 daemon；Ctrl+C 仅关闭自己创建的服务。自定义远端地址仍需自行启动。
4. 页面确认“调度后端：OpenFang”。选择 Robot 02，注入故障，选择“模型 AI”，勾选修复并开始诊断。
5. 查看任务报告：backend 应为 openfang，模型调查记录使用 OpenFang 模型，最终查看复测误差和独立位置验收。规则模式始终不调用 OpenFang。

切回原模式只需设置 `RDL_AGENT_BACKEND=direct` 并重启。
页面显示所选后端，不代表 daemon 健康检查已经通过。连接或模型配置失败会使任务显式失败，不会降级伪装成 AI 成功。

## 协议与权限

适配依据：[官方 REST API](https://github.com/RightNow-AI/openfang/blob/main/docs/api-reference.md)、[官方 Agent manifest](https://github.com/RightNow-AI/openfang/blob/main/agents/hello-world/agent.toml)。

使用 POST /api/agents 创建、POST /api/agents/{id}/message 调查、DELETE /api/agents/{id} 清理。
这是应用层 JSON 消息协议：模型返回 calls，网关仅允许 inspect_graph、read_parameters、sample_motion、inspect_peer、finish_diagnosis，再把真实工具结果送回模型。
没有把 ROS 注册为 OpenFang 原生 MCP 工具。远端 manifest 不授予原生工具、网络工具、共享记忆或派生 Agent 权限。
官方内核把空 capabilities.tools 解释为不限制工具，因此 manifest 使用非空占位工具名，并把同一个名字同时放进 tool_allowlist 和 tool_blocklist，使最终工具集合为空；不能简化为 tools = []。实现依据是官方 kernel.rs 的 available_tools_with_registry 过滤逻辑。
最多六轮，每轮最多五个工具请求；错误 JSON 显式失败。引用验证、机器人范围、修复前检查、修复白名单、独立 Pose 验收仍由本地代码执行。

机器人实时运动由原有 ROS 控制器完成。自然语言入口由 OpenFang 解析自定义导航目标；手动诊断入口保持目标 (8,8)。修复后的复测总是使用该任务的原目标。
取消会请求 ROS 停止并阻止后续修复；正在等待的远端请求可能直到 60 秒请求超时后才释放任务锁。
远端删除失败记录 warning 和 agent_id；进程被强制结束或创建请求响应丢失时，可能留下远端 Agent，需在 OpenFang 中检查清理。

## 验证范围

新增 MockTransport 契约测试覆盖 manifest、消息往返、token 统计、非法调用、清理和错误脱敏；另覆盖独立 Agent 范围和规则工作流。
2026-10-27 本地实测 OpenFang 0.6.9 + DeepSeek deepseek-flash + ROS 2 Lyrical：三类故障全部诊断、修复、复测通过，且三次任务 backend 均为 openfang，远端调查 Agent 清理后无残留。详见 [本次验收记录](openfang-validation.md)。这是单次小样本验证，不代表通用准确率。
尚未实现：复杂多阶段任务规划、OpenFang 原生 agent-to-agent 调度、跨机器心跳/任务租约、Gazebo/Nav2、代码补丁生成部署、实体机器人。不要据此宣称生产效率提升。
