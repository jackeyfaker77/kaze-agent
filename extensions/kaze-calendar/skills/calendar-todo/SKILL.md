---
name: calendar-todo
description: 读取 ICS 日程、管理本地待办和设置提醒；用于查询安排、截止日期、完成任务及维护日历订阅。
---

# 日历和待办

工具由 `calendar` MCP 提供。日历订阅只读；本地任务保存到独立 SQLite 数据库，不同步账号服务。

- 先用 `calendar_manage(action="list")` 确认订阅；新增需要真实 ICS 链接。Google 日历可提供私有 iCal 地址，不要把私有地址写入公开消息、版本库或日志。
- 通过 `calendar_query(start_at, end_at)` 查询日程。时间范围必须含偏移，区间右端不包含，单次最多93天。返回 `fresh=false` 时明确说明缓存过期，可用 `poll_calendars(force=true)` 刷新后再判断。
- `todo_create` 新建任务；`todo_list` 读取；`todo_update(todo_id, changes)` 修改、完成、软删除或重新打开。日期用明确偏移，例如 `2026-10-20T15:00:00+08:00`，不要传未解析的自然语言或无时区时间。
- 用户只给日期时，先结合已有偏好确定提醒时刻；无法确定时询问。不要默认在凌晨打扰用户。
- 有截止时间且不传提醒时间时，在截止时间产生一次提醒。可明确指定较早的 `reminder_at`。无截止也无提醒则只记录。改期时说明新的截止/提醒时间。
- `status="completed"` 仅用于用户明确表示已完成；`deleted` 为软删除；`open` 重新打开并重新启用提醒。推送提醒不代表任务已完成。

## 主动提醒

`get_proactive_events` 的 `alert` 候选交给 Kaze 统一处理，使用目标会话 session_key 作为 consumer。不要在 HEARTBEAT 或调度任务中另开轮询/发送循环。

候选的 `event_id` 是精确版本；`item_id` 是日程实例或任务的稳定身份。发送前由宿主调用 `validate_proactive_events` 核对版本；无效版本不能用于推送，暂时不可核实则保留等待恢复。发送失败不 ACK，成功处理才确认对应版本。ACK 不改变任务完成状态。

定时日程默认提前15分钟，全天日程按日历时区当天09:00，忽略 ICS 内的 VALARM。日程提醒在开始15分钟后过期，全天提醒在当天结束时过期；待办提醒在截止/提醒时间24小时后过期。过期任务仍能查询，不会每天重复提醒。设置和主动检查周期可能造成延迟；需要准点执行的单次指令使用宿主的 schedule 工具。
