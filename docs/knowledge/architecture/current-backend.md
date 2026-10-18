---
title: 后端运行结构
kind: 架构说明
status: 当前有效
last_verified_commit: 3572ff26
source_paths:
  - apps/backend/bootstrap/tools.py
  - apps/backend/bootstrap/app.py
  - apps/backend/agent/looping/core
  - apps/backend/agent/turns/orchestrator.py
  - apps/backend/agent/core/passive_turn
  - apps/backend/bus/chat_lane.py
  - apps/backend/desktop_bridge/session_service.py
related:
  - overview.md
  - boundaries-and-evolution.md
  - ../flows/startup-and-shutdown.md
  - ../flows/passive-turn.md
---

# 后端运行结构

后端的装配中心是 `bootstrap/tools.py`，应用生命周期由 `bootstrap/app.py` 驱动。业务模块通过显式依赖共享同一套会话、工具、渠道和事件服务。

## 核心对象

| 对象 | 职责 |
| --- | --- |
| AppRuntime | 启停 CoreRuntime、渠道和后台循环 |
| CoreRuntime | 持有 AgentLoop、Provider、总线、会话、工具、Scheduler、记忆、MCP 和插件 |
| AgentLoop | 执行被动回合，管理执行任务、取消、会话锁和运行状态 |
| TurnOrchestrator | 组织回合结果和输出，提供发送后历史提交 |
| SessionManager / SessionStore | 缓存、更新和持久化会话与消息 |
| MessageBus / EventBus | 分别传递输入输出消息和进度、生命周期、推送同步事件 |
| ProcessingState / ChatLane | 分别记录忙状态和协调同一传输目标的交互与推送 |
| ProactiveKernel / Scheduler | 分别执行主动判断和定时任务 |
| DesktopBridgeService | 处理桌面 RPC、装配会话快照并输出事件 |

## Agent 结构

`agent/looping/core/` 包提供 AgentLoop 的装配、处理、流式输出和中断逻辑。`agent/core/passive_turn/` 提供被动回合管线和推理循环，`agent/lifecycle/` 提供各阶段上下文与 hook，`agent/tools/` 提供工具实现及注册、执行设施。

插件通过 PluginManager 注册能力与生命周期扩展。MCP 工具与本地工具进入共享工具目录；主动插件的能力声明、目录快照和执行图由 ProactiveKernel 使用。

## 三类执行入口

| 入口 | 执行方式 | 历史与投递 |
| --- | --- | --- |
| 桌面 chat.send | Bridge → process_direct | 管线保存普通回合历史，Bridge 发出 chat.done |
| 渠道消息 | MessageBus → AgentLoop.run | 管线保存普通回合历史，输出再由总线发送 |
| 主动 / 定时推送 | Kernel 或 Scheduler → MessagePushTool | 发送成功后向目标会话提交历史并通知桌面 |

Scheduler soft 使用临时执行会话，目标上下文、忙状态与最终历史仍关联保存的目标 session_key。主动执行图使用专用候选处理和决策工具。

## 同步边界

AgentLoop 的会话锁保护回合执行，SessionManager 的写锁保护消息更新。ProcessingState 是计数状态；ChatLane 是渠道目标协调器，按 canonical channel / chat_id 汇合渠道交互与后台推送。

`conversation/push_sync.py` 订阅图片推送及 AfterReasoning 事件，将图片附到所属回合，或保存独立的图片消息。文本推送历史由 TurnOrchestrator 提交。桌面会话快照直接从会话管理服务构建。
