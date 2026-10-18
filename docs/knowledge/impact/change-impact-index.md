---
title: 改动影响索引
kind: 修改指南
status: 当前有效
last_verified_commit: 3572ff26
source_paths:
  - apps/backend/bootstrap/tools.py
  - apps/backend/agent/looping/core
  - apps/backend/agent/turns/orchestrator.py
  - apps/backend/session
  - apps/backend/desktop_bridge
  - apps/backend/proactive_v2
  - apps/desktop/renderer/src/SessionApp.tsx
related:
  - ../map.md
  - ../architecture/boundaries-and-evolution.md
  - ../modules/outbound-and-push-sync.md
---

# 改动影响索引

修改时按数据归属定位实现，再检查入口、持久化和桌面展示。以下列出主要联动范围。

| 修改对象 | 联动模块 | 验证重点 |
| --- | --- | --- |
| session_key 与渠道目标 | SessionManager、Bridge、channel_identifiers、ProcessingState、ChatLane、Scheduler、主动宿主 | 执行 key、历史 key、归一化目标各自正确 |
| 会话 schema 或消息字段 | SessionStore、SessionManager 缓存、回合持久化、桥接快照、工作台 | 写入后读取一致，媒体和工具链完整 |
| 身份或全局配置 | config_models、workspace、上下文构建、Bridge、设置界面 | 新会话身份与模型配置正确，记忆范围一致 |
| 普通回合 | AgentLoop、passive_turn、生命周期、Provider、工具、Bridge / 渠道输出 | 锁、中断、流式结果、历史和输出状态 |
| 后台推送 | MessagePushTool、ChatLane、TurnOrchestrator、图片同步、EventBus、Bridge | 成功回执先于历史提交与通知，失败保留正确状态 |
| 调度 | Scheduler、调度工具、bootstrap 回调、任务 RPC / 表单 | 时区、下一次触发、无状态执行、目标上下文、运行次数及关闭等待 |
| 主动与 Drift | bootstrap/proactive、Kernel、能力目录、生命周期与流程插件、源目录、状态库 | 三路源、规则、门控、去重、ACK、历史与通知 |
| 记忆引擎 | core/memory、选用的插件、memory2、agent/retrieval、Markdown 运行时 | 查询与写入范围、后台整理、关闭及配置选择 |
| 渠道 | Channel 合约、ChannelHub、平台适配、Session key / reply context、总线、ChatLane | 输入身份、白名单、目标解析、发送回执与协调 |
| Provider | provider 层、传输、授权、推理循环、配置、模型界面 | 工具流、超时、取消、错误及授权 |
| 桌面 RPC | models、dispatcher、service、writer、bridgeClient、renderer | 请求响应、事件关联、会话快照和并发边界 |
| 语音 | voice handler / service、Electron controller、播放与录音、设置 | ASR 入会话、TTS、turn 关联、取消及错误 |
| 桌宠 | PetPackageService、导入校验、Bridge、renderer/pet | 包格式、全局选择、动作和回复显示 |
| 启停与持久化资源 | AppRuntime、CoreRuntime、Scheduler、插件、MCP、HTTP、SessionStore | 后台任务结束顺序与发送后提交完成 |

## 文档同步

- 入口或依赖变化：更新 [总体架构](../architecture/overview.md) 和相关流程页。
- 数据模型或所有权变化：更新 [会话](../modules/conversations-and-sessions.md)、[工作区数据](../data/local-workspace.md) 和对应模块页。
- 能力增减或模块改名：更新 [能力地图](../map.md)、知识库入口、source_paths 与相关链接。
- 运行协调变化：同时检查 [运行协调说明](../../runtime-coordination.md)，区分队列消费、执行锁、忙状态和发送协调的实际效果。
