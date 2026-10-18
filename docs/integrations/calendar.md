# 日历和待办

`extensions/kaze-calendar` 是可独立安装的 MCP 服务，提供只读 ICS 日历订阅和本地待办。服务负责缓存、查询和产生提醒候选；Kaze 通过已有的主动入口、ChatLane 和 TurnOrchestrator 决定并完成投递，真实发送成功后提交会话历史。

## 安装与注册

在仓库根目录，使用 Python 3.12 或更新版本运行：

```powershell
.venv/Scripts/python.exe extensions/kaze-calendar/scripts/prepare.py --workspace workspace --register --enable-proactive
```

脚本创建独立 `.venv`、安装依赖，将服务注册为 `calendar`，并安装 `calendar-todo` 工作区技能。已有 MCP 服务、主动源和文件中的其他字段会被保留；遇到同名冲突或用户修改过的技能会停止，避免覆盖。可用 `--skip-install` 复用已经准备的环境，`--skip-skill` 保留自定义技能。

配置默认保存在 `workspace/mcp-data/kaze-calendar/config.json`，SQLite 数据库默认与配置文件同目录。`database_path` 相对配置文件解析。也可通过 `--config` 指定已有配置。重启后端后加载新注册；仍需在 Kaze 中配置主动目标并启用主动功能。

新增主动源声明如下：

```json
{
  "server": "calendar",
  "channel": "alert",
  "get_tool": "get_proactive_events",
  "ack_tool": "acknowledge_events",
  "validate_tool": "validate_proactive_events",
  "enabled": true
}
```

## 工具

| 工具 | 用法 |
| --- | --- |
| `calendar_manage` | `list`、`subscribe`、`pause`、`resume`、`unsubscribe`；订阅需要名称和真实 HTTP(S)/webcal ICS 地址 |
| `calendar_query` | 按起止时间查询缓存日程，单次最多93天，通过 `limit`/`offset` 翻页 |
| `poll_calendars` | 手动刷新，每批最多4个来源；`remaining` 非零时可继续刷新 |
| `calendar_status` | 查看版本、来源同步时间、失败情况、缓存新鲜度和待办数量 |
| `todo_create` | 新建任务，支持标题、备注、截止时间和提醒时间 |
| `todo_list` | 按 `open`、`completed`、`deleted`、`all` 查询本地任务 |
| `todo_update` | 用精确 `todo_id` 和 `changes` 修改字段、完成、软删除或重新打开 |
| `get_proactive_events` | 获取当前已到提醒时间、未过期、未被指定会话消费的精确版本 |
| `validate_proactive_events` | 核对提醒是否仍有效，不改变消费状态 |
| `acknowledge_events` | 确认精确版本已处理；不改变任务完成状态 |

例如，对 Kaze 说“添加待办：周五下午三点前改完简历，提前半小时提醒我”，模型应将日期解析为明确时间后调用：

```json
{
  "title": "改完简历",
  "due_at": "2026-10-23T15:00:00+08:00",
  "reminder_at": "2026-10-23T14:30:00+08:00"
}
```

完成时调用 `todo_update(todo_id, {"status":"completed"})`；重新打开用 `open`，删除用 `deleted`。无截止或提醒时间的任务仅保存。只设截止时间时，提醒默认跟随截止时间；自定义提醒时间不会随改期自动变动。用 `null` 清除时间字段。提醒不能晚于截止时间。

## 时间与同步约定

- 待办和查询参数使用含明确偏移的 ISO 8601 时间，不接受无时区时间。数据库和输出时间统一为 UTC，提醒正文附带配置时区的时间。
- ICS 的 TZID 和日历时区优先；没有日历时区时，订阅 `timezone` 兜底，默认 `Asia/Shanghai`。支持全天日程、RRULE/RDATE/EXDATE、单次改期和取消，以及 IANA 时区的夏令时变化。
- 定时日程默认提前15分钟提醒；全天日程按日历时区当天09:00提醒，可在服务配置修改。第一版使用订阅的统一提醒规则，不读取 ICS 内的 VALARM。
- 日程提醒到开始后15分钟过期，全天日程在当天结束时过期；待办提醒到截止/提醒时间24小时后过期。过期任务仍可查询，不会自动每天重发；完成后重新打开会产生新的提醒版本。
- 后台默认每5分钟刷新，支持 ETag/Last-Modified。只有完整解析成功后才同时更新缓存和 HTTP 条件请求标记。单源失败不影响其他来源和本地任务；默认30分钟后缓存失效，仍可查询旧日程，但暂停生成日历提醒。
- ICS 是完整日历快照：取消、删除和改期以最近成功获取的订阅内容为准，源服务和本地轮询均可能有更新延迟。Google Calendar 可提供私有 iCal 订阅地址；本服务不需要 Google 登录，也不能写回源日历。私有地址只存本地数据库，工具列表和同步错误不回显地址。
- 每个 ICS 限2 MiB、2000个 VEVENT；单次展开最多5000个实例。拒绝按秒/分钟重复的日程，不支持导入 ICS VTODO、邀请应答或云端待办账号。

提醒由主动检查触发，还受 Kaze 的目标、准入策略和投递状态约束，不能保证准点抵达。需要精确执行时间的单次命令，仍使用宿主的 schedule 工具。

## 版本、去重与发送前核对

日程实例或待办有稳定 `item_id`，提醒的 `event_id` 为 `item_id@revision`。版本由有效内容和提醒时间计算；重复读取、重复同步不会生成新版本。循环日程的每次实例有独立身份。ACK 按精确版本和目标会话持久化；晚到的旧版本 ACK 不会消费更新后的提醒，也不会影响其他会话。

默认主动链路在裁决后核对候选；Wake 在读取持久化提醒后及模型生成后核对。两条链路还会在进入发送协调区之后、真正发送之前再核对，拦住等待期间已失效的提醒。`valid_event_ids` 表示有效版本；`unavailable_event_ids` 表示缓存暂时无法核实，应保留等待恢复。已取消、改期、完成、删除、消费或过期的旧版本不能发送。未配置 `validate_tool` 的已有源保留原有协议和路由身份。

发送失败不 ACK，源端候选保留；发送成功才运行本地投递去重、历史提交和 ACK。ACK 失败时，默认链路由本地投递去重阻止重复发送并重试 ACK；Wake 先本地消费并排队重试 ACK。普通 ACK 使用 `processed`；只有显式提供真实平台回执才允许 `delivered`。

## 验证

```powershell
extensions/kaze-calendar/.venv/Scripts/python.exe -m pytest -q extensions/kaze-calendar/tests
.venv/Scripts/python.exe extensions/kaze-calendar/scripts/check_kaze.py --backend apps/backend --server-python extensions/kaze-calendar/.venv/Scripts/python.exe
```

第二条通过真实 stdio MCP 和本地 HTTP 日历夹具验证注册、三通道共存、提醒版本、改期/完成失效、发送失败保留、成功历史提交和 ACK 恢复。测试只使用临时工作区及模拟投递，不向实际聊天渠道发送消息。
