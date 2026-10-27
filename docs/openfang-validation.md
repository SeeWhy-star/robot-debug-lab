# OpenFang 真实联调记录

执行日期：2026-10-27（America/Los_Angeles）。
环境：OpenFang 官方 Linux x86_64 v0.6.9（发行包 SHA256 校验通过），DeepSeek deepseek-flash，ROS 2 Lyrical，双 TurtleSim。

运行 `scripts/acceptance.py`，通过页面同一 FastAPI 接口注入故障并提交 AI 任务。
随后逐项读取任务，确认 backend=openfang、status=verified、没有清理 warning；OpenFang /api/agents 中没有残留 rdl- 调查 Agent。

| 场景 | 结果 | 修复后位置误差 | Run ID |
| --- | --- | --- | --- |
| 正常导航 | 通过 | 0.0260 | — |
| 速度话题接错 | 通过 | 0.1643 | f1f7b09ba157 |
| 速度上限为零 | 通过 | 0.0188 | fc4899765e18 |
| 控制器停止 | 通过 | 0.1655 | 93bb7f3951a3 |

三次故障任务修复前均失败，误差 3.479。最终通过阈值为新鲜 Pose 与目标距离 < 0.22。
模型输入/输出 token 分别为 10527/594、10565/757、10163/608；这是 OpenFang 响应报告的用量，不含费用估算。
完整本地报告见 .runtime/acceptance.json，任务证据保存在 SQLite，可从网页按 Run ID 查阅和导出。

模型文字仍需核对：例如本次零速度诊断提及“线速度/角速度”都受 max_speed 限制，而代码只限制线速度；此措辞不准确，但参数证据、根因分类和独立位置验收均成立。不能把自然语言解释全部当作事实。

26 项 Python 测试通过；9 项真实 API 检查通过，覆盖写入令牌、未知机器人、越界目标、双任务并发、忙碌冲突、取消隔离、锁释放与证据导出。API 测试结束后两台机器人已重置。此次只验证已有三个预设故障，不包括 OpenFang Supervisor 导航规划、任意代码修复、实体机器人或大样本准确率。

## 日常启动

当前版本推荐一条命令启动 OpenFang 和机器人调试台（配置见 [接入说明](openfang.md)）：

```bash
bash scripts/start_all.sh
```

打开 http://127.0.0.1:8765。选择模型 AI，按原有步骤注入故障并诊断。
Ctrl+C 关闭本次启动器创建的服务；复用的外部 OpenFang 不会被关闭。
