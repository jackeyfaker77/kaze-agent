---
title: Agent 生命周期与工具
kind: 模块说明
status: 当前有效
last_verified_commit: 3572ff26
source_paths:
  - apps/backend/agent/looping/core
  - apps/backend/agent/core/passive_turn
  - apps/backend/agent/lifecycle
  - apps/backend/agent/tools/registry.py
  - apps/backend/agent/tool_hooks/executor.py
  - apps/backend/agent/plugins/manager.py
  - apps/backend/agent/mcp/registry.py
  - apps/backend/bootstrap/toolsets
  - apps/backend/bootstrap/tools.py
  - apps/backend/agent/screen_observation/service.py
  - apps/backend/plugins/observe/plugin.py
related:
  - ../flows/passive-turn.md
  - ../flows/model-provider.md
  - memory.md
  - proactive-and-drift.md
  - ../architecture/overview.md
---

# Agent 生命周期与工具

AgentLoop 管理回合任务和运行状态；被动管线负责上下文、推理及持久化。工具、插件与 MCP 在装配时进入共享运行时，再按当前回合配置决定可见能力。

## 生命周期

主要阶段为 BeforeTurn → BeforeReasoning → 模型与工具循环 → AfterReasoning → AfterTurn。prompt_render 负责提示词，before_step / after_step 围绕每次模型与工具步骤执行。生命周期上下文携带当前会话、结果、媒体和工具链等信息。

bootstrap 将进度接到 EventBus 与活动回合快照，Bridge 再转为流式正文和工具事件。修改 hook 时需核对阶段顺序、异常传播、取消和后续持久化是否仍能完成。

## 工具与技能

ToolRegistry 汇集本地工具及 MCP 工具；工具目录、搜索、参数 schema 与执行设施共同控制模型能发现和调用的能力。`bootstrap/toolsets/` 创建工具并注入工作区、会话、记忆、Scheduler、渠道或共享 HTTP 服务。

文件、Shell、网络、记忆、消息推送、调度和 spawn 等工具在各自实现中处理参数与结果。技能由 skills 模块加载并加入回合上下文。spawn 的后台任务由 `agent/background/` 管理，完成事件可通过输入总线返回。

## 插件与 MCP

PluginManager 加载配置启用的插件，收集生命周期模块、工具、门控和主动能力声明。MCP Registry 管理连接并将工具接入同一目录。新增能力需同时确认配置启用、工具可见和实际入口调用。

主动执行由 ProactiveKernel 使用插件提供的 lifecycle、runtime / module factory 和能力快照；普通被动回合沿 Agent 生命周期运行。

## 屏幕观察与桌宠动作

屏幕观察由 `agent/screen_observation/` 提供服务与记忆适配，`infra/screen_capture.py` 处理截图。`bootstrap/tools.py` 创建观察服务并注册 observe_screen 工具，桌面 observation RPC 也调用相应服务。

observe 插件订阅回合提交、检索与记忆写入事件，记录运行诊断和错误；屏幕截图与观察服务由上述独立模块提供。

desktop_pet 插件提供 pet_action，发布动作事件给桌面显示。具体能力受配置、插件启用和当前工具上下文约束。
