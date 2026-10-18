---
title: 渠道系统
kind: 模块说明
status: 当前有效
last_verified_commit: 3572ff26
source_paths:
  - apps/backend/bootstrap/channels.py
  - apps/backend/bootstrap/channel_host.py
  - apps/backend/core/channels/hub.py
  - apps/backend/infra/channels/contract.py
  - apps/backend/infra/channels/telegram_channel
  - apps/backend/infra/channels/qq_channel
  - apps/backend/plugins/qqbot
  - apps/backend/core/common/channel_identifiers.py
  - apps/backend/bus/queue.py
related:
  - ../flows/passive-turn.md
  - outbound-and-push-sync.md
  - conversations-and-sessions.md
---

# 渠道系统

渠道适配器把平台输入转换为 InboundMessage，并把总线输出或主动推送转换成平台调用。bootstrap 与 ChannelHost 根据配置启动、注册和关闭渠道。

## 合约与依赖

`infra/channels/contract.py` 定义 Channel 的 start / stop 合约。ChannelContext 注入消息总线、会话、事件、推送工具、附件存储、共享 HTTP、中断控制和 ChannelHub。

平台适配位于 Telegram、QQ 模块及 qqbot 插件。输入权限由配置和工作区 channels.json 中的发送者白名单控制。附件通过渠道公共存储提供给回合。

## 消息流

1. 渠道收到平台消息，解析接收方、发送者、正文和附件。
2. InboundMessage 通过 MessageBus 进入 AgentLoop.run，会话默认按 channel / chat_id 关联。
3. Agent 保存回合历史并发布 OutboundMessage。
4. 总线输出分发器调用相应渠道发送；后台推送使用 MessagePushTool 取得发送回执。

渠道 AgentLoop.run 当前按队列逐项等待执行；桌面通过 Bridge 直接进入 process_direct。判断并行行为时，需要分别检查这两个入口。

## 目标解析与协调

渠道名和接收方通过 channel_identifiers 与渠道 resolve_target 归一化。ChatLane 用这一结果协调被动交互与后台推送，确保输入和发送指向同一个目标 lane。

MessagePushTool 依赖渠道注册结果。新增渠道时需接通普通输出、主动 / 调度发送、目标解析和回执处理，并验证成功后的历史与桌面通知。

渠道负责平台适配。会话消息由 SessionManager 保存，身份、长期记忆与全局配置由各自工作区服务提供。
