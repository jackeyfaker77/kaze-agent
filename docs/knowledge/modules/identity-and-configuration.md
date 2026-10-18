---
title: 身份与工作区配置
kind: 模块说明
status: 当前有效
last_verified_commit: 3572ff26
source_paths:
  - apps/backend/agent/config_models.py
  - apps/backend/agent/config.py
  - apps/backend/agent/context.py
  - apps/backend/agent/core/prompt_block.py
  - apps/backend/prompts/agent.py
  - apps/backend/agent/persona.py
  - apps/backend/core/common/workspace.py
  - apps/backend/desktop_bridge/session_service.py
related:
  - conversations-and-sessions.md
  - memory.md
  - desktop-and-bridge.md
  - ../architecture/overview.md
---

# 身份与工作区配置

Kaze 的配置与身份以工作区为范围。会话 key 管理独立的消息记录，身份提示词、长期记忆、主模型、语音与桌宠选择由工作区统一提供。

## 标识与配置

| 对象 | 标识或位置 | 用途 |
| --- | --- | --- |
| 桌面会话 | 新建时生成 `desktop:<uuid>` | 保存消息、标题及会话元数据 |
| 外部渠道会话 | 默认 `<channel>:<chat_id>` | 将渠道输入关联到历史 |
| 后台执行 | Scheduler soft 的 `scheduler:{job.id}` | 登记临时执行任务与中断状态 |
| 推送目标 | 目标 session_key 加渠道 / chat_id | 关联历史、忙状态与实际发送 |
| 全局配置 | 工作区 `config.toml` 与 Config 模型 | 模型、记忆、语音、主动及工具配置 |
| 桌宠选择 | 工作区 `pets/settings.json` | 保存应用当前选择的桌宠包 |

InboundMessage 可通过 metadata.session_key_override 指定会话归属。桥接接受 session_key，也兼容 chat_id 参数。传输目标经渠道 resolve_target 归一化后用于 ChatLane。

## 身份与上下文

普通回合由 ContextBuilder 组合内置身份与行为提示词、共享 Markdown 记忆、当前会话历史和检索结果。主动与 Drift 通过 `agent/persona.py` 读取 `memory/VEDA.md`，其次读取工作区 `SOUL.md`，没有自定义文件时使用基础身份规则。

`PROACTIVE_CONTEXT.md` 在主动 tick 中明确加载；`HEARTBEAT.md` 可补充主动任务规则。身份文件与主动规则分别提供身份和行为约束。

## 全局设置

模型目录第一个注册项为主模型，具体 Provider 由 bootstrap 构建。记忆引擎由 memory.enabled / memory.engine 决定；语音服务及 TTS voice_id 使用全局 voice 配置。桌宠包保存在统一的 pets 目录。

改变会话 key 时，应同时核对缓存、历史、后台任务、渠道回执和桌面选中状态。改变身份或记忆范围时，应同时核对普通回合、主动上下文、检索与写入路径。
