---
title: 记忆系统
kind: 模块说明
status: 当前有效
last_verified_commit: 3572ff26
source_paths:
  - apps/backend/bootstrap/memory.py
  - apps/backend/core/memory/engine.py
  - apps/backend/core/memory/runtime.py
  - apps/backend/core/memory/markdown
  - apps/backend/agent/retrieval
  - apps/backend/plugins/default_memory
  - apps/backend/plugins/akasha
  - apps/backend/memory2
related:
  - conversations-and-sessions.md
  - identity-and-configuration.md
  - agent-lifecycle-and-tools.md
  - ../data/local-workspace.md
---

# 记忆系统

会话历史按 session_key 保存，长期记忆以工作区共享。bootstrap 同时装配 Markdown 记忆层与配置选用的记忆引擎，Agent 通过统一契约查询和写入。

## Markdown 层

MarkdownMemoryRuntime 使用工作区的统一 MemoryStore，读取长期记忆、自我信息、近期上下文及历史。MEMORY.md、HISTORY.md、PENDING.md、SELF.md 等文件位于 `workspace/memory/`。

Markdown 层在 build_memory_runtime 中始终构建。它的 maintenance / consolidation 负责追加、整理及后台任务，共享写锁保护工作区文档更新。会话元数据不会自动将这些文件切成不同存储。

## 记忆引擎

memory.enabled 控制引擎启用，memory.engine 选择实现，默认名为 default：

- default_memory 使用 memory2 提供的存储、检索和增强处理能力。
- akasha 提供图与激活相关检索、消息索引及诊断。
- 未启用引擎时使用 DisabledMemoryEngine，Markdown 层仍提供共享文档上下文。

MemoryEngine 的查询、结果和写入契约位于 `core/memory/engine.py`，MemoryRuntime 将引擎与 Markdown 运行时组合。具体数据库位置和增强参数由对应插件配置决定。

## 回合中的记忆

`agent/retrieval/` 将记忆查询适配为回合检索结果，BeforeTurn 准备上下文，推理阶段消费检索内容。回合后的写入与整理由相应生命周期和当前引擎的插件处理。

主动与 Drift 复用宿主记忆服务。消息向量缓存可写入 sessions.db，供主动兴趣原型等功能复用；向量缓存与完整会话历史具有不同的数据用途。

## 修改影响

切换引擎或修改 schema 时核对插件选择、检索注入、消息索引、写入、后台整理和关闭。改变共享范围时，应同步检查身份、Markdown 文档、语义检索和主动上下文；会话消息隔离本身不会改变全局记忆的范围。
