---
title: 调度任务
kind: 领域说明
status: 当前有效
last_verified_commit: 9ef475e3
source_paths:
  - apps/backend/agent/scheduler.py
  - apps/backend/agent/scheduler_cron.py
  - apps/backend/agent/tools/schedule.py
  - apps/backend/bootstrap/toolsets/schedule.py
  - apps/backend/desktop_bridge/session_service.py
  - apps/backend/bus/chat_lane.py
related:
  - proactive-and-drift.md
  - desktop-and-bridge.md
---

# 调度任务

`apps/backend/agent/scheduler.py` 负责计算触发时间和调度执行，`apps/backend/agent/tools/schedule.py` 将创建、查询、更新和取消能力暴露给 Agent，bootstrap 将工具注册进 ToolRegistry。桌面桥接提供任务管理 RPC，任务的目标会话保存在 `ScheduledJob.session_key`。

`instant` 直接生成投递正文；`soft` 通过 `AgentLoop.process_direct(stateless=True)` 在 `scheduler:{job.id}` 内部执行，不读写会话历史。两种任务均经过共享 ChatLane 和 MessagePushTool，真实发送成功后由宿主提交到保存的目标会话，再发布桌面通知。忙状态按目标会话 key 计数，内部执行 key 用于任务登记与取消。一次性任务完成后终止；周期任务根据上次计划时间稳定计算下一次触发，避免进程延迟造成连续补发。

新建与更新任务默认固定为 `Asia/Shanghai`，不读取宿主系统时区；调用方仍可显式传入其他 IANA 时区。cron 星期字段遵循 POSIX 语义：`0` 和 `7` 都表示周日。无论安装了 APScheduler 还是走内置 fallback，都会得到相同的星期解释。

## 修改影响

- 修改任务 schema：检查工具参数、持久化、bridge models、presenter 和桌面表单。
- 修改触发计算：检查时区、夏令时、错过执行、重复执行和重启恢复。
- 修改任务目标：检查保存的会话 key、传输目标归一化、工具权限和投递后的历史归属。
- 修改取消/暂停：确认 scheduler runtime 与持久化状态同时更新。

## 验收重点

覆盖一次性与周期任务、时区、重启恢复、取消、重复触发保护、空响应和模型失败、真实发送失败、调度历史隔离、成功历史提交以及桌面状态刷新。发送优先级及跨会话并行的独立结论见 [运行协调说明](../../runtime-coordination.md)。
