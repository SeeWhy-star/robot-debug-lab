# Project Brief

## 项目描述

Robot Debug Lab 是面向 ROS 2 仿真系统的多 Agent 故障诊断平台。针对机器人运行失败后需要人工切换命令行、检查节点连接和参数、反复复测的问题，将任务复现、模型工具调查、受限修复和独立位置验收组织为可追溯工作流。

## 技术栈

Python / FastAPI / rclpy / ROS 2 Lyrical / TurtleSim / 自定义 Action / OpenAI-compatible tool calling / DeepSeek / SQLite WAL / SSE / React / TypeScript / Vite / pytest。

## 可用于简历的描述

- 构建双机器人命名空间隔离的 ROS 2 调试平台，设计 Assistant 网关与独立 Agent 上下文，实现任务分发、机器人互斥和并行调查。
- 将节点拓扑、参数读取、运动采样封装为受限工具，建立带证据引用的诊断协议、调用预算和错误传播机制。
- 设计“复现—调查—修复—复测”闭环，通过新鲜 Pose 与目标误差独立判定修复结果，避免依赖模型自报成功。
- 实现实时轨迹看板、SSE 事件流、SQLite 任务历史和 JSON 证据导出，支持人工验收和实验回放分析。

## 面试时需要讲清楚

它体现分布式服务的职责拆分，但当前部署是单机多进程 + 线程池；不能说已实现分布式调度集群。AI 负责诊断，20 Hz 控制由确定性 ROS 控制器完成。自动修复为三个白名单动作，不是任意代码自愈。真实模型端到端实验样本很小，不能据此宣称生产准确率、效率提升百分比或通用故障覆盖率。OpenFang、Gazebo/Nav2、RAG 和实体机器人属于后续工作。
