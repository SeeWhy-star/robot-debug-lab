# Validation

本地验收日期：2026-10-23（UTC）。环境为 Ubuntu 26.04、ROS 2 Lyrical、TurtleSim、DeepSeek `deepseek-flash`。以下是一次完整验收运行，不能视为统计准确率。

| 场景 | 结果 | 复测误差（仿真单位） | Run ID |
|---|---|---|---|
| healthy-navigation | 通过 | 0.1696 | — |
| topic_mismatch | 通过 | 0.1669 | 478a20e93423 |
| zero_speed | 通过 | 0.1765 | 9c75032241aa |
| controller_stopped | 通过 | 0.1632 | 405ca7c6b389 |

另外 9 项 pytest 测试通过；9 项 API 检查通过：无令牌写入拒绝、未知机器人、越界目标、双任务并发、忙碌冲突、取消接受、取消隔离、锁释放、证据导出。前端 TypeScript 检查与 Vite 构建通过。

完整机器可读记录见 `docs/acceptance-results.json` 与 `docs/acceptance-api-results.json`；每次重新验收在 `.runtime/` 写新记录。记录中模型诊断是当时的原始输出，可能有措辞不准确之处；最终是否恢复以独立 validation 结果为准。

构建期间实际发现并修复了 ROS Action float 字段接收整数导致进程中止的问题，以及嵌入式 ROS 信号处理和子进程生命周期问题。现将目标显式转换为 float，桥接不注册全局信号处理，子进程使用独立监督器。

尚未验收：OpenFang 编排、跨机器 DDS、实体机器人、Gazebo/Nav2、其他 ROS 发行版、长时间稳定性和模型大样本准确率。浏览器验收覆盖本地桌面视图，不代表完整移动端兼容性验证。

桌面浏览器已实测：注入话题故障 → 选择规则基线 → 开始诊断 → 验证通过（run `64b86d0b37fa`）；历史真实 AI 报告可选择并显示证据。停止服务后已确认所有本项目 ROS 子进程退出，随后重启成功并保留历史记录。
