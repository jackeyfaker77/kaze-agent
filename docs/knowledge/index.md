---
title: Kaze 项目知识库
kind: 知识库入口
status: 当前有效
last_verified_commit: 3572ff26
source_paths:
  - apps/backend/main.py
  - apps/backend/bootstrap
  - apps/backend/session
  - apps/backend/agent
  - apps/backend/proactive_v2
  - apps/backend/desktop_bridge
related:
  - map.md
  - architecture/overview.md
  - impact/change-impact-index.md
---

# Kaze 项目知识库

这里说明 Kaze 的模块职责、执行入口、状态归属和修改影响。阅读时先定位能力，再沿流程进入 source_paths 对应的源码。

## 导航

- [总体架构](architecture/overview.md)：应用装配、被动消息、主动推送与状态归属。
- [后端运行结构](architecture/current-backend.md)：核心对象、执行入口和同步边界。
- [架构边界与修改原则](architecture/boundaries-and-evolution.md)：依赖方向与扩展约束。
- [能力地图](map.md)：按能力查找模块和知识页。
- [改动影响索引](impact/change-impact-index.md)：查找需要一起修改和验证的模块。

## 模块与流程

| 内容 | 知识页 | 阅读重点 |
| --- | --- | --- |
| 身份与配置 | [身份与工作区配置](modules/identity-and-configuration.md) | 会话标识、全局身份、模型及配置归属 |
| 会话与消息 | [会话与消息](modules/conversations-and-sessions.md) | SessionManager、SQLite、缓存和历史更新 |
| 推送与同步 | [输出与推送同步](modules/outbound-and-push-sync.md) | 回执、发送后提交、图片附加和桌面通知 |
| Agent 与工具 | [Agent 生命周期与工具](modules/agent-lifecycle-and-tools.md) | 回合阶段、工具注册、插件与 MCP |
| 记忆 | [记忆系统](modules/memory.md) | Markdown、记忆引擎、检索和写入范围 |
| 主动与 Drift | [主动行为](modules/proactive-and-drift.md) | Default / Wake、取源、去重、Drift 和投递 |
| 调度 | [调度任务](modules/scheduling.md) | instant / soft、目标上下文、触发和关闭 |
| 渠道 | [渠道系统](modules/channels.md) | 输入总线、渠道合约、目标解析和发送协调 |
| 桌面 | [桌面端与桥接](modules/desktop-and-bridge.md) | Electron、JSONL RPC、会话快照与桌宠 |
| 语音 | [语音交互](modules/voice.md) | ASR、会话输入、TTS、播放和取消 |
| 本地数据 | [本地工作区数据](data/local-workspace.md) | 持久化文件、数据归属和资源关闭 |

- [启动与关闭](flows/startup-and-shutdown.md)
- [被动回合](flows/passive-turn.md)
- [模型 Provider](flows/model-provider.md)
- [运行协调](../runtime-coordination.md)

文档是源码摘要。实现调整时，同步修改对应模块页、流程页和导航，并核对 source_paths 与相关链接。
