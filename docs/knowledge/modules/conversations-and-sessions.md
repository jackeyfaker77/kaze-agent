---
title: 会话与消息
kind: 模块说明
status: 当前有效
last_verified_commit: 3572ff26
source_paths:
  - apps/backend/session/manager/manager.py
  - apps/backend/session/manager/models.py
  - apps/backend/session/manager/persistence.py
  - apps/backend/session/manager/projection.py
  - apps/backend/session/store/connection.py
  - apps/backend/session/store/messages.py
  - apps/backend/desktop_bridge/session_service.py
  - apps/backend/conversation/push_sync.py
related:
  - identity-and-configuration.md
  - outbound-and-push-sync.md
  - ../flows/passive-turn.md
  - ../data/local-workspace.md
---

# 会话与消息

SessionManager 管理以 session_key 索引的会话对象、缓存和写入；SessionStore 使用工作区 `sessions.db` 保存会话元数据与消息。桌面列表、详情和工作台读取这一套记录。

## 数据结构

Session 包含消息列表与元数据。SQLite 的 sessions / messages 表保存会话标题、消息序号、正文、媒体、推理信息、工具链、主动标识及送达相关字段；同一会话的消息按序号排列。

thread_id 等字段是消息或任务的附加元数据。当前会话存储和查询入口由 session 模块提供，`conversation/` 只处理外部图片推送同步。

## 读写与一致性

- SessionManager 缓存活跃会话，并按 key 使用写锁串行化持久化更新。
- 普通回合在 AfterReasoning 保存用户与助手消息及工具链。
- 主动和调度推送取得成功回执后，由 TurnOrchestrator.commit_sent_turn 追加目标会话的助手消息。
- `session/manager/projection.py` 将最新助手消息的送达标记同步到存储与缓存。
- 图片推送同步服务复用 SessionManager，将回合所属图片附到消息，或追加独立的后台图片消息。

会话执行锁位于 AgentLoop，保护同一 key 的回合执行；这里的写锁保护持久化更新。两者参与不同阶段。

## 桌面操作

DesktopBridgeService 提供 session.draft、create、list、get、rename、delete。draft 只选择一个 key，普通空白草稿在发送消息前无需创建持久化记录。

会话快照由桥接从 SessionManager 构建，再通过 RPC 结果或 chat.done、message.pushed 等事件传给桌面。删除操作更新持久化记录并失效缓存；共享长期记忆另由记忆系统维护。

## 修改影响

消息字段变化需同步存储读写、缓存、回合提交、桌面快照和工作台。投递状态变化需同时检查发送回执、成功历史提交以及图片同步，避免同一推送重复保存。
