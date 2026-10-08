---
title: 主动行为
kind: 领域说明
status: 当前有效
source_paths:
  - apps/backend/bootstrap/proactive.py
  - apps/backend/proactive_v2/loop.py
  - apps/backend/agent/core/proactive_kernel.py
  - apps/backend/proactive_v2/lifecycle.py
  - apps/backend/proactive_v2/frame.py
  - apps/backend/proactive_v2/state.py
  - apps/backend/proactive_v2/mcp_sources.py
  - apps/backend/proactive_v2/source_catalog.py
  - apps/backend/plugins/default_proactive
  - apps/backend/plugins/proactive_flow
  - apps/backend/plugins/wake_proactive
  - apps/backend/plugins/wake_proactive_flow
  - apps/backend/plugins/drift_flow
  - apps/backend/plugins/wake_drift_flow
related:
  - conversations-and-sessions.md
  - scheduling.md
---

# 主动行为

生产调用链为 AppRuntime.start → prepare_proactive_loop → run_proactive → KazeProactiveLoop → ProactiveKernel。插件声明的 lifecycle、runtime factory 与 module factory 构成执行图；同一轮和运行中 Kernel 持有能力快照 lease。普通 Agent 的 process_direct、NO_REPLY / USED 文本协议不再参与主动回合。

## 生命周期

Default 运行准入、三路取源、路由、专用 Agent 工具、统一裁决与投递阶段，包含正文预取、引用验证、SQLite 投递键去重、近期主动文案语义去重，以及完整 Drift 分支。电量用于调度，AnyAction 持久化额度和成功推送间隔。

Wake 先将候选写入持久化 reservoir，content 在本地接管后即可上游 ACK；alert 成功发送后消费并排队 ACK，失败队列跨 tick 和重启恢复。只有新内容启动内容概率判断，结合全池价值、兴趣原型、新鲜度、来源多样性和唤醒抑制期。筛选、并行调查、share / skip 使用结构化工具和候选别名。

Context 的可信状态变化受全局节流，触发重新评估或单条场景判断。Wake Drift 采用持久化 next_attempt_at，进入完整 Drift 活动管线。没有活动或没有有价值的提案时保持安静。

## 宿主适配

Kaze 保留会话和真实渠道服务、已有插件门控与共享 MCP；新增插件能力声明只服务于主动链路。工作区 JSON 源编译为同一源目录，没有固定 Feed 名称依赖。PROACTIVE_CONTEXT.md 每轮实际加载，可选 HEARTBEAT.md 作为附加规则。

身份读取 memory/VEDA.md 或 SOUL.md；互动向量复用现有记忆引擎的 embedding 客户端，补齐 canonical 消息缓存。完整被动回合参与原型，未回复、未来及主动消息排除。

投递先取渠道回执，成功后提交实际助手历史、presence 和成功副作用；桌面随后通知。用户在判断过程中回复可取消投递。

## 状态与排查

- proactive.db：Default tick / step、投递键、场景限额和 Drift 时间。
- proactive_quota.json：Default 的 AnyAction 额度。
- wake_proactive.db：候选池、ACK 重试、Wake / Context / Drift 驱动与观察记录。
- drift/drift.db：Drift 活动运行和探索状态。
- sessions.db：成功主动助手消息及可复用消息向量。

模型决策记录与渠道成功历史各有含义，不能用 reply 决策代替送达证明。旧简化入口的 policy、源连接池和 JSON 去重模块只保留兼容接口，没有生产调用。

配置、行为边界和回归点详见 [主动推送指南](../../_handbook/proactive-guide.md)，迁入范围与宿主差异见 [源码来源](../../third-party/akashic-proactive.md)。
