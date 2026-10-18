---
title: 输出与推送同步
kind: 模块说明
status: 当前有效
last_verified_commit: 3572ff26
source_paths:
  - apps/backend/agent/turns/orchestrator.py
  - apps/backend/agent/turns/outbound.py
  - apps/backend/agent/tools/message_push.py
  - apps/backend/bus/queue.py
  - apps/backend/bus/chat_lane.py
  - apps/backend/conversation/push_sync.py
  - apps/backend/desktop_bridge/session_service.py
related:
  - conversations-and-sessions.md
  - channels.md
  - scheduling.md
  - proactive-and-drift.md
---

# 输出与推送同步

普通回复、主动推送与调度消息共用会话和渠道设施，各自的历史提交时点由宿主链路决定。MessagePushTool 负责后台发送，TurnOrchestrator 负责将成功输出提交到目标会话。

## 输出路径

| 输出 | 发送路径 | 历史提交与通知 |
| --- | --- | --- |
| 桌面普通回复 | Bridge 直接调用 AgentLoop | AfterReasoning 保存，随后 chat.done 提供快照 |
| 渠道普通回复 | 回合输出进入 MessageBus | 回合历史先保存，输出分发器随后发送 |
| 主动 / 调度正文 | MessagePushTool | 成功回执后 commit_sent_turn，再通知桌面 |
| 外部图片 | MessagePushTool 与 ExternalImagePushed 事件 | 按图片归属附加到回合或保存后台图片消息 |

生成完成、历史已写入、平台已接收与桌面已刷新分别反映不同阶段。

## 发送协调

共享 ChatLane 按归一化后的 channel / chat_id 管理目标。被动交互具有优先权，后台发送按队列协调；发送窗口覆盖实际投递以及宿主成功提交，避免用户在窗口内回复时与后台推送错序。

主动结果可携带 `delivery_guard`，TurnOrchestrator 在进入协调区之后、调用实际发送之前执行。日历和待办用它重新核对候选版本；检查不通过时不发送、不写成功历史，也不执行成功副作用。

目标 session_key 决定历史归属，传输目标决定消息发往哪里。Scheduler soft 的临时执行 key 只用于运行登记；正文使用保存的目标上下文并提交到目标历史。

## 图片同步

`conversation/push_sync.py` 的 ExternalImageSyncService 订阅 ExternalImagePushed 和 AfterReasoningCtx：

- attach_to_turn：暂存图片，在对应回合持久化时附加媒体。
- already_persisted：核对最近的助手消息已有该图片及匹配的传输信息。
- commit_after_delivery：交给主动宿主在投递完成后统一提交正文和媒体。
- 独立后台图片：追加到目标会话，保存后发布 ProactiveMessageCommitted。

该服务的存储依赖是 SessionManager。它负责图片与消息的同步，文本主动或调度历史通过 TurnOrchestrator 提交。

## 桌面事件

Bridge 监听流式、工具与推送提交事件，输出 chat.delta、工具事件、chat.done 和 message.pushed。推送需要延迟到历史提交完成后通知时，pending / committed 回调负责连接两个阶段，保证桌面读取到已保存的快照。

验收需覆盖发送失败、历史提交失败、媒体归属、重复回写和用户插入交互，详细边界见 [运行协调说明](../../runtime-coordination.md)。
