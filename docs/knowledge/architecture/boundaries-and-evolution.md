---
title: 架构边界与修改原则
kind: 架构说明
status: 当前有效
last_verified_commit: 3572ff26
source_paths:
  - apps/backend/bootstrap/tools.py
  - apps/backend/agent/turns/orchestrator.py
  - apps/backend/session/manager
  - apps/backend/core/memory
  - apps/backend/core/channels/hub.py
  - apps/backend/desktop_bridge/session_service.py
related:
  - overview.md
  - current-backend.md
  - ../impact/change-impact-index.md
---

# 架构边界与修改原则

模块边界以当前状态归属和调用链为依据。扩展能力时，先找到负责数据和操作的模块，再在 bootstrap 中注入依赖。

## 模块分工

| 层 | 负责内容 | 调用边界 |
| --- | --- | --- |
| bootstrap | 构建对象、注入依赖、启停服务 | 调用业务模块提供的接口 |
| agent | 上下文、推理、工具循环、回合结果 | 通过会话、记忆、工具和输出接口访问外部能力 |
| session | 会话与消息的缓存及持久化 | 提供查询、更新、保存和送达标记 |
| core/memory | 记忆查询和写入契约、Markdown 运行时 | 由配置选用的插件实现检索与整理 |
| core/channels 与 infra/channels | 渠道注册、目标解析、协议和平台适配 | 输入输出经总线或 MessagePushTool 进入运行链路 |
| proactive_v2 与主动插件 | 取源、决策、去重、额度、Drift 与投递 | 复用宿主的记忆、工具、会话和渠道服务 |
| desktop_bridge | RPC 校验、事件输出、视图快照 | renderer 的业务操作通过此边界进入后端 |
| conversation | 图片推送同步 | 复用 SessionManager 和生命周期事件 |

## 扩展时需要保持的约束

- 执行 key、目标 session_key 和传输目标各有用途。后台任务的临时 key 用于任务登记，目标 key 用于上下文、忙状态与历史归属，渠道目标用于发送协调。
- 普通回合和后台推送的历史提交时点不同。修改输出路径时，同时核对回合持久化、渠道回执和桌面通知。
- 会话消息按 key 隔离，Markdown 长期记忆与身份配置属于工作区。改变记忆隔离范围需要同时修改检索和写入。
- 渠道接收方先经 resolve_target 归一化，再参与 ChatLane。新增渠道需让输入与推送采用同一解析结果。
- 新插件能力必须接到实际执行入口；注册、目录可见性与运行调用分别验证。
- 持久化字段的调整应覆盖读写、缓存、桥接快照和桌面展示。

## 修改与验收

先在 [改动影响索引](../impact/change-impact-index.md) 定位联动模块，再检查对应流程页。运行协调变更以 [运行协调说明](../../runtime-coordination.md) 的入口和状态边界为验收范围；跨会话并行需要逐个入口确认消费者、任务调度和资源共享方式。
