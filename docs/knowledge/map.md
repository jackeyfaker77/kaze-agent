---
title: 能力地图
kind: 模块索引
status: 当前有效
last_verified_commit: 3572ff26
source_paths:
  - apps/backend/bootstrap/tools.py
  - apps/backend/agent
  - apps/backend/session
  - apps/backend/core
  - apps/backend/conversation
  - apps/backend/plugins
  - apps/desktop/renderer/src/SessionApp.tsx
related:
  - index.md
  - architecture/overview.md
  - impact/change-impact-index.md
---

# 能力地图

## 入口与运行

| 能力 | 主要源码 | 说明 |
| --- | --- | --- |
| 启动与装配 | `main.py`、`bootstrap/app.py`、`bootstrap/tools.py` | [启动与关闭](flows/startup-and-shutdown.md) |
| 被动回合 | `agent/looping/core/`、`agent/core/passive_turn/` | [被动回合](flows/passive-turn.md) |
| 模型调用 | `bootstrap/providers.py`、`agent/provider.py`、`agent/model_runtime/` | [模型 Provider](flows/model-provider.md) |
| 工具、技能、MCP、插件 | `agent/tools/`、`skills/`、`plugins/`、`bootstrap/toolsets/` | [Agent 生命周期与工具](modules/agent-lifecycle-and-tools.md) |
| 输入输出与事件 | `bus/queue.py`、`bus/event_bus.py`、`bus/chat_lane.py` | [渠道](modules/channels.md)、[输出与同步](modules/outbound-and-push-sync.md) |

以上源码路径以 `apps/backend/` 为根目录。

## 数据与后台能力

| 能力 | 主要源码 | 说明 |
| --- | --- | --- |
| 配置与身份 | `agent/config_models.py`、`core/common/workspace.py` | [身份与工作区配置](modules/identity-and-configuration.md) |
| 会话与消息 | `session/manager/`、`session/store/` | [会话与消息](modules/conversations-and-sessions.md) |
| 记忆 | `core/memory/`、`memory2/`、`plugins/default_memory/`、`plugins/akasha/` | [记忆系统](modules/memory.md) |
| 外部图片同步 | `conversation/push_sync.py` | [输出与推送同步](modules/outbound-and-push-sync.md) |
| 定时任务 | `agent/scheduler.py`、`agent/tools/schedule.py` | [调度任务](modules/scheduling.md) |
| 主动判断 | `proactive_v2/`、`agent/core/proactive_kernel.py`、主动插件 | [主动行为](modules/proactive-and-drift.md) |
| Drift | `plugins/drift_flow/`、`plugins/wake_drift_flow/` | [主动行为](modules/proactive-and-drift.md) |
| 屏幕观察 | `agent/screen_observation/`、`infra/screen_capture.py`、`agent/tools/observe_screen.py` | [Agent 生命周期与工具](modules/agent-lifecycle-and-tools.md) |
| 运行诊断 | `plugins/observe/` | [Agent 生命周期与工具](modules/agent-lifecycle-and-tools.md) |

## 桌面与渠道

| 能力 | 主要源码 | 说明 |
| --- | --- | --- |
| 桌面桥接 | `apps/backend/desktop_bridge/` | [桌面端与桥接](modules/desktop-and-bridge.md) |
| 桌面界面 | `apps/desktop/renderer/src/SessionApp.tsx` | [桌面端与桥接](modules/desktop-and-bridge.md) |
| Electron 进程通信 | `apps/desktop/src/bridge/bridgeClient.ts` | [桌面端与桥接](modules/desktop-and-bridge.md) |
| 语音 | `apps/backend/desktop_bridge/voice/`、`apps/desktop/src/voice/` | [语音交互](modules/voice.md) |
| 桌宠包 | `apps/backend/core/pets/packages.py`、`apps/desktop/renderer/src/pet/` | [桌面端与桥接](modules/desktop-and-bridge.md) |
| Telegram / QQ / 渠道插件 | `apps/backend/infra/channels/`、`apps/backend/core/channels/hub.py`、`apps/backend/plugins/qqbot/` | [渠道系统](modules/channels.md) |

本地文件与 SQLite 的归属见 [工作区数据](data/local-workspace.md)；涉及多个模块的修改见 [改动影响索引](impact/change-impact-index.md)。
