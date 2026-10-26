# 事件事实 → 可重建会话投影

后续增量：[可重建执行状态与人工处置](execution-projection-v1.md) 已补齐终态投影、
工作台异常执行入口和显式重试边界。本文保留会话投影阶段的范围记录。

本阶段把会话历史的权威来源移到既有
`private_runtime/execution/events.sqlite3` 的 `events` 表。
执行事实和会话事实共享 root_id、全局 seq 和追加写保护；`sessions.db`
中的会话、消息、搜索索引、来源索引和重放位置成为可重建的物化结果。
本地 bridge、独立 Host 和远程桌面继续使用原 SessionStore / SessionManager 接口。

## 参考 Maka 源码后的取舍

阅读基于本地 Maka `59f61cf7d`，重点包括：

- `packages/core/src/runtime-event-store.ts`：权威写入失败必须报错、稳定事件身份和终态边界。
- `packages/storage/src/sqlite-runtime-store.ts`：事件接受、重复身份校验及可重建索引。
- `packages/runtime/src/runtime-event-read-model.ts`：确定性投影、消息来源和硬错误诊断。
- `packages/runtime/src/runtime-ledger-repair.ts`、`runtime-event-backfill.ts`：分批导入旧历史、保留旧消息身份，不能推断缺失的工具执行事实。
- `packages/runtime/src/terminal-run-commit.ts`、`continuation-replay.ts`：权威终态和安全续跑边界。
- `packages/runtime/src/__tests__/runtime-continuation-crash.test.ts`：在持久化边界真实杀死进程。

本阶段采用这些边界，未复制其 TypeScript 实现，也未一次性迁移模型上下文、
终态索引和恢复续跑。后续上下文投影可以读取同一事实空间，不能将本阶段称为完整
Event-Sourced Runtime。

## 事实与提交顺序

`conversation.baseline` 和 `conversation.changed` 是版本化事件批次。
批次包含 `session.recorded`、`message.recorded`、`session.deleted`、
`message.deleted`。消息保留稳定 ID、seq、时间、正文、工具链、媒体、扩展属性和投递回执。
已删除消息的历史事实仍然保留，但投影重放删除事件后不会恢复到可见会话。

现有写入接口先在未提交事务内完成校验和变更计算，通过连接私有临时触发器收集
受影响身份；转换成业务事实后，以 SQLite WAL / FULL synchronous 提交到事件库。
随后才提交会话表、消息来源索引和重放位置。触发器只是兼容现有写入接口的收集器，
事实中不保存可执行 SQL，纯投影函数也不执行模型、工具或外部发送。

两个数据库之间不假设原子提交：

1. 事实提交前失败：会话事务回滚，没有成功接纳该变更。
2. 事实提交后、视图提交前失败：事实仍是权威；下一次访问或重启先补齐视图。
3. 视图提交后退出：消息和位置在同一事务内，重复重放不再次追加。

每条会话事件带内容摘要和前一事件的位置／摘要，重建校验连续前缀。
未知事件版本、摘要不一致、来源不匹配或缺失前缀会显式失败，不能返回看似成功的空历史。
读取投影前补齐积压事实；普通读操作不会追加新事实，也不会调用模型。

`conversation_message_sources` 可将可见消息追溯到最近一次决定其内容的事实 seq。
执行期间的会话事件另带 `execution_turn_id`，既有 `messages.committed` 仍作为执行审计事件保留，
不会再次参与会话重放。补存采用稳定 `session_write_id`，避免事实已接受而返回失败时换 ID 再插入。

## 旧历史与迁移

第一次打开现有 SessionStore 时，在排他写事务下读取历史，按最多 256 条／约 4 MiB
分页形成基线；单条超大消息保留完整内容。所有基线页与迁移标识在事件库一次提交。
退出发生在提交之前可以重做，提交之后按原基线重建，不重复导入。
基线明确标注 `legacy_snapshot`，保留原 ID、顺序、时间、元数据和字段；不会从聊天文本
编造旧模型调用、工具完成回执或执行成功状态。

迁移索引可由已提交的基线重建。旧执行日志不自动再次转成会话消息，否则可能与旧历史重复。
新产生的流式 `reply.delta` 仍保留在执行日志，尚未成为会话写入的片段不冒充完整聊天回复；
原取消补存流程和中断记录查询继续保留。自动续跑、人工处置界面不在本阶段范围内。

## 恢复与维护

正常重启自动补齐已提交的会话事实。要显式重建整个会话视图，先停止使用该工作区的后端：

```powershell
.venv/Scripts/python.exe apps/backend/main.py conversation-rebuild --workspace ./workspace
```

命令取得同一 Host 工作区锁；后端运行时返回退出码 2，避免影响内存会话缓存。
它不加载配置、模型或渠道。原数据库损坏时，应先备份并移走损坏的视图及其 WAL/SHM，
再从完整事件库恢复；不要移走事件库。仅清除会话表／索引可通过原地重建修复。

保留现有本地和远程连接方式、配置、主动提醒频率和 Telegram 投递策略。
迁移后不应让不认识事件事实的旧版本继续写同一工作区。备份必须保留完整事件库、
附件和既有待办／队列等独立状态；会话投影不是这些外部状态的替代品。

## 验证

- `tests/backend/session/test_conversation_projection.py`：旧历史、CRUD、来源、搜索、迁移分页、
  双连接、丢失视图、缺失源、损坏前缀、写失败及同进程重试。
- `tests/e2e/conversation_projection_crash.py`：基线提交后、事实提交后、视图提交前和提交后
  使用 `os._exit(73)`，重复启动／重建校验消息和位置，不依赖 finally 清理。
- 既有本地桌面生命周期与远程 Host / Calendar MCP 探针验证模式兼容。
