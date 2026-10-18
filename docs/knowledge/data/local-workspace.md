---
title: 本地工作区数据
kind: 数据说明
status: 当前有效
last_verified_commit: 3572ff26
source_paths:
  - apps/backend/core/common/workspace.py
  - apps/backend/session/manager/manager.py
  - apps/backend/session/store/connection.py
  - apps/backend/bootstrap/toolsets/schedule.py
  - apps/backend/bootstrap/memory.py
  - apps/backend/core/pets/packages.py
  - apps/backend/agent/model_runtime/auth/store.py
related:
  - ../modules/conversations-and-sessions.md
  - ../modules/memory.md
  - ../modules/scheduling.md
  - ../modules/proactive-and-drift.md
---

# 本地工作区数据

开发版默认使用仓库的 workspace 目录，安装版使用 `%USERPROFILE%\.hasaki\workspace`；HASAKI_WORKSPACE 可指定其他位置。实际路径由工作区解析和启动配置确定。

## 文件与归属

以下路径以实际工作区为根目录。可选功能在启用并运行后创建相应状态。

| 路径 | 所属模块与用途 |
| --- | --- |
| `config.toml` | Config：模型、记忆、语音、主动及工具设置 |
| `sessions.db` | SessionStore：会话与消息；presence 和复用消息向量也保存在此库 |
| `memory/` | 共享 MEMORY、HISTORY、PENDING、SELF、RECENT_CONTEXT 等 Markdown 文档 |
| `memory/memory2.db` | default 记忆引擎的默认数据库，可通过插件配置覆盖 |
| `memory/akasha.db` | akasha 记忆引擎的默认数据库，可通过插件配置覆盖 |
| `memory/VEDA.md`、`SOUL.md` | 主动与 Drift 的身份文件，按优先顺序读取 |
| `PROACTIVE_CONTEXT.md`、`HEARTBEAT.md` | 主动规则及可选附加任务 |
| `schedules.json` | Scheduler 的任务定义与执行状态 |
| `proactive.db`、`proactive_quota.json` | Default 的诊断、投递去重、场景 / Drift 状态和额度 |
| `wake_proactive.db` | Wake 候选池、ACK 重试、驱动与观察状态 |
| `drift/drift.db` | Drift 活动与探索状态 |
| `channels.json` | 渠道发送者白名单 |
| `mcp_servers.json` | MCP 连接配置 |
| `pets/`、`pets/settings.json` | 桌宠包与应用当前选择 |
| `.desktop/codex-auth.bin` | Codex 授权存储，Windows 使用当前用户 DPAPI |
| `.desktop/user-data/` | Electron 用户数据、缓存与桌面诊断 |

## 数据边界

sessions.db 的消息记录按 session_key 隔离。身份、Markdown 长期记忆、模型、语音和桌宠选择属于工作区。删除会话由会话服务操作数据库和缓存；共享记忆及其他后台状态由各自模块维护。

## 关闭与备份

SessionStore 在应用资源关闭阶段释放连接。Scheduler 先等待已进入的发送后提交，再关闭会话和事件资源。

备份应保留工作区配置、会话数据库、记忆、调度和主动状态文件。运行中的 SQLite 使用其备份接口，或在应用关闭后复制，确保数据库及附属文件一致。Windows 授权文件由当前用户加密，迁移到其他用户后需要重新授权。
