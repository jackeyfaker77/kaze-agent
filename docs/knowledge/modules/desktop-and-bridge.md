---
title: 桌面端与桥接
kind: 模块说明
status: 当前有效
last_verified_commit: 3572ff26
source_paths:
  - apps/desktop/renderer/src/SessionApp.tsx
  - apps/desktop/src/bridge/bridgeClient.ts
  - apps/backend/desktop_bridge/server.py
  - apps/backend/desktop_bridge/request_dispatcher.py
  - apps/backend/desktop_bridge/session_service.py
  - apps/backend/desktop_bridge/stream_writer.py
  - apps/backend/core/pets/packages.py
  - apps/desktop/renderer/src/pet
related:
  - conversations-and-sessions.md
  - outbound-and-push-sync.md
  - identity-and-configuration.md
  - voice.md
---

# 桌面端与桥接

React 的 SessionApp 提供会话、工作台、记忆、模型、任务和设置界面。Electron 主进程通过 bridgeClient 启动 Python 后端，以 JSONL stdio 传递请求、结果和事件。

## 后端边界

DesktopBridgeServer 读取请求，request_dispatcher 管理分发、并发和取消入口，DesktopBridgeService 调用运行时服务，stream_writer 串行输出 JSONL。

主要 RPC 包括会话草稿和 CRUD、chat.send / cancel、消息搜索、记忆读写、任务查询、运行目录、文档、桌宠、屏幕观察。voice.* 和 codex.* 分别委托给对应服务。

chat.send 调用 AgentLoop.process_direct，复用被动管线和会话锁。Bridge 为请求登记会话关联，接收流式与工具事件，最终输出 chat.done 及会话快照。

## 桌面数据

会话列表、消息详情与工作台从 sessions.db 对应服务读取；Bridge 负责快照和 JSON 字段装配。当前工作台提供查询与工具链查看，消息历史的业务更新由后端执行。

主动和调度推送完成历史提交后，Bridge 发布 message.pushed，桌面再更新对应会话。活动回合与正在显示的会话通过 key 和请求 ID 关联。

## 桌宠与语音

PetPackageService 校验和导入 ZIP 包，保存到工作区 pets 目录，settings.json 记录应用选中项。renderer/pet 根据状态、动作事件和回复内容驱动动画与气泡。

语音由 Electron 录音、播放和热键模块配合后端 voice 服务完成，使用全局语音配置，并将识别结果发送到当前会话。详细数据流见 [语音交互](voice.md)。

## 修改影响

修改 RPC 时同步后端 models / service、Electron bridgeClient 和 renderer 的类型与事件处理。修改请求生命周期时核对排队、取消、流式结果、chat.done 和快照；修改推送事件时核对历史是否已经提交。
