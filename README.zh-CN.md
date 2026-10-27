# Robot Debug Lab

[English](README.md) | 简体中文

一个可以运行、观察和复现的 ROS 2 多机器人 AI 调试实验室。

每个机器人拥有独立的诊断上下文，由 Assistant 网关协调任务。Agent 读取真实 ROS 节点、话题、参数和运动状态，用证据提出故障判断；网关执行受限修复，再用实际位置检验原任务是否恢复。无需购买机器人。

## 当前能力

- 两台独立命名空间的 TurtleSim：`robot_1`、`robot_2`。
- 自定义 ROS 2 Navigate Action；控制器 20 Hz 发布 Twist，模型不参与实时控制。
- 三类真实故障：控制器进程停止、速度话题配错、速度参数为零。
- OpenAI-compatible Chat Completions 工具调用；已实际接入 DeepSeek。
- 独立 Agent 调查、只读工具、证据编号、固定修复白名单、同目标复测。
- React 调试台：实时轨迹、任务状态、取消、历史记录、JSON 证据导出。
- 自然语言 Assistant：指定单/双机器人目标，导航失败自动诊断，获准修复后重试同一目标。见 [使用示例](docs/assistant.md)。
- 有限澄清续接、计划历史和刷新恢复；支持直连模型或 OpenFang 后端，一条命令启动服务。
- 显式规则模式用于离线基线；模型失败不会自动伪装成 AI 诊断。

**实现边界：** Assistant 用模型生成单次导航计划，由程序化网关校验和调度；每台机器人拥有独立 Agent 实例与任务上下文，仍在同一服务中运行。支持模型直连与 OpenFang REST 后端，已完成真实导航规划和故障恢复联调。尚无多轮复杂任务规划、任意代码修复或实体机器人接入。配置步骤见 [OpenFang 接入](docs/openfang.md)，结果见 [联调记录](docs/openfang-validation.md)。

## 启动

已验证环境：Ubuntu 26.04 amd64、ROS 2 Lyrical Desktop、系统 Python 3.14、Node.js 和 npm、uv、colcon。ROS Python 扩展必须使用匹配的系统 Python。本版本使用 Lyrical 的 `turtlesim_msgs`，尚未适配 Humble/Jazzy。

```bash
git clone https://github.com/SeeWhy-star/robot-debug-lab.git
cd robot-debug-lab
# 初次安装；已有 .venv 时无需重建
uv venv --system-site-packages --python /usr/bin/python3 .venv
uv pip install --python .venv/bin/python -r requirements.lock
bash scripts/build.sh
npm --prefix web ci
npm --prefix web run build
# 仅在没有 .env 时复制，保留已有凭据
[ -f .env ] || cp .env.example .env
chmod 600 .env
# 在本地编辑 .env 配置模型；不要提交密钥
bash scripts/start_all.sh
```

打开 <http://127.0.0.1:8765>。以后只需执行 `bash scripts/start_all.sh`。选择 OpenFang 后端时会自动启动本机 daemon，或复用已运行的服务；首次安装见 [接入说明](docs/openfang.md)。Ctrl+C 只停止本次启动器创建的服务。日志位于 `.runtime/lab-service.log` 和 `.runtime/openfang-service.log`。默认关闭原生 TurtleSim 窗口；`RDL_NATIVE_GUI=1 bash scripts/start_all.sh` 可显示原生窗口。

直接体验：在顶部输入“让机器人 2 去 (7,6)”，点击“解析并执行任务”，再点击“查看执行结果”。缺少机器人或坐标时可补充信息继续，刷新后能找回计划。任务成功以位置容差验收为准，不要求坐标小数完全相等。

模型配置：`LLM_BASE_URL`（API 根地址，不含 `/chat/completions`）、`LLM_MODEL`、`LLM_API_KEY`。修改后重启。选择服务端支持工具调用的模型。当前项目凭据只在本地 `.env`，前端拿不到模型密钥。

## 三分钟验收

1. 保持 Robot 01 正常，选择 Robot 02，注入“话题不匹配”。
2. 使用 AI 模式、勾选自动修复，点击开始诊断。原始导航会超时，这是故障复现的一部分。
3. 查看模型调查过程和证据、修复动作，以及复测的位置误差；状态应为“验证通过”。
4. 更换为速度为零、控制器停止，分别重复。也可以关闭自动修复，只查看诊断建议。
5. 离线时切换规则模式；它不调用模型，不作为 AI 效果证明。

AI 调查会发送当前任务文本、仿真节点/话题/参数/位置到配置的模型服务。每轮最多 6 次模型请求、每次最多 5 个工具调用、输出上限 1800 tokens。界面记录服务端返回的 token 用量；未提供费用估算。

## 验证命令

```bash
source scripts/env.bash
.venv/bin/python -m pytest -q
# 服务启动后执行；会重置仿真并调用真实模型
.venv/bin/python scripts/acceptance.py
# 服务启动后执行；并发、取消、权限检查，不调用模型
.venv/bin/python scripts/acceptance_api.py
# 自然语言规划验收；会重置仿真并调用模型
.venv/bin/python scripts/acceptance_assistant.py
# 只读浏览器冒烟检查
cd web
npx playwright install chromium
npm run test:ui
```

当前版本通过 44 项 Python 测试、9 项真实 API 检查、前端构建及桌面/手机浏览器检查，详见 [浏览器验收](docs/ui-qa.md)。真实模型已覆盖三类预设故障；这是小样本功能验收，不代表生产准确率。

结果保存在 `.runtime/`。历史任务存在 SQLite；重启后未完成任务标为 interrupted，不会自动恢复动作。ROS 默认隔离到本机 domain 93，可用 `RDL_DOMAIN_ID` 修改。不要在同一 domain 同时启动多个副本。

## 文件结构

```text
robot_debug_lab/  FastAPI、Agent、工作流、ROS 桥接、控制器
ros_ws/src/      自定义 Navigate Action 接口
web/             React + TypeScript + Vite 调试台
scripts/         构建、启动、真实验收
tests/          契约与行为测试
docs/           架构、验收、项目说明
```

更多内容：[架构与边界](docs/architecture.md)、[验收记录](docs/validation.md)、[面试项目描述](docs/project-brief.md)。原始代码采用 MIT License，依赖保留各自许可证。

## 隐私与安全

仅用于本机仿真。不要通过公网、隧道或反向代理暴露服务。`.env`、日志、数据库、ROS 构建产物和本地截图不会提交。AI 模式会把任务文本和仿真观测发送到你配置的模型服务；规则模式不调用模型。导出报告前仍应检查是否包含私密任务文本。

请使用自己的 API Key，仅保存在本地 `.env`；不要发到聊天、Issue、截图或提交中。泄露后应在服务商后台撤销并换新。详见 [Security & Privacy](SECURITY.md)。
