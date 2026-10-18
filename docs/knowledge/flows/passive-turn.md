---
title: 被动回合流程
kind: 流程说明
status: 当前有效
last_verified_commit: 3572ff26
source_paths:
  - apps/backend/agent/looping/core/processing.py
  - apps/backend/agent/looping/core/assembly.py
  - apps/backend/agent/core/passive_turn/pipeline.py
  - apps/backend/agent/core/passive_turn/reasoner.py
  - apps/backend/agent/core/passive_turn/reasoning_loop.py
  - apps/backend/agent/turns/orchestrator.py
  - apps/backend/desktop_bridge/session_service.py
related:
  - ../modules/agent-lifecycle-and-tools.md
  - ../modules/conversations-and-sessions.md
  - ../modules/outbound-and-push-sync.md
---

# 被动回合流程

桌面与外部渠道共享被动回合管线，但进入 AgentLoop 的方式不同。桌面 chat.send 通过 process_direct；渠道输入由 AgentLoop.run 从 MessageBus 队列取出。

## 执行阶段

1. AgentLoop 在对应传输目标的 ChatLane 被动窗口内取得 session_key 的会话锁，登记执行任务，并由 ProcessingState 记录忙状态。
2. BeforeTurn 加载会话，准备身份、近期历史和记忆检索上下文。
3. BeforeReasoning 准备工具上下文、技能和可见能力。
4. Reasoner 渲染提示词，执行模型与工具循环；before_step / after_step 和工具事件在相应边界更新进度。
5. AfterReasoning 解析回复，追加普通用户、助手消息及工具链；图片同步服务可在此阶段补充回合媒体。
6. AfterTurn 执行生命周期收尾。桌面发送 chat.done 和会话快照；渠道输出进入 MessageBus，由输出分发器发送。

## 状态与取消

回合任务、中断信息及活动快照由 AgentLoop 管理。桌面取消通过桥接找到请求对应的任务，触发中断和任务取消；上下文、工具执行与模型流式调用需要共同处理中断。

会话执行锁与持久化写锁分别保护回合顺序和历史更新。总线消费者目前等待当前回合结束后再处理下一项；桌面直接请求使用各自执行任务，不经过这个输入队列。

## 输出与历史

普通回合在 AfterReasoning 保存历史，渠道发送发生在后续输出阶段。因此生成成功、历史存在和平台发送成功是不同状态。

主动与定时推送使用发送后提交路径，详见 [输出与推送同步](../modules/outbound-and-push-sync.md)。调整普通回复输出时，不应直接套用后台推送的持久化时点。
