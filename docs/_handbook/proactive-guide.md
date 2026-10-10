# Kaze 主动推送

Kaze 的生产入口使用 Akashic 的完整主动 Kernel，提供 `wake` 与 `default` 两条生命周期。专用主动工具完成候选判断和结束决策，统一投递编排器负责渠道发送、成功历史和副作用。入口是 `apps/backend/bootstrap/proactive.py`，不再调用普通 Agent 解析 `NO_REPLY` / `USED:`。

## 开启

在实际工作区的 `config.toml` 中配置，重启后端或完全退出并重新打开桌面端：

```toml
[proactive]
enabled = true
lifecycle = "wake"
session_key = "desktop:daily"
default_channel = "desktop"
default_chat_id = ""
profile = "daily"

[proactive.agent]
model = ""
max_steps = 35
content_limit = 5
web_fetch_max_chars = 8000
context_prob = 0.03
delivery_cooldown_hours = 1

[proactive.drift]
enabled = false
max_steps = 20
min_interval_hours = 3
```

桌面目标为空时使用 `session_key`；外部渠道填写渠道名与真实 `default_chat_id`。会话决定上下文归属。主动检查默认关闭，Kaze 加载配置时默认选择 Wake；显式 `lifecycle = "default"` 切换到电量策略。

桌面“高级”设置提供策略、目标、主动判断模型和 Drift 开关。Default 另外提供检查预设、配额和语义去重开关。保存设置保留自定义 profiles、overrides，以及 agent / drift 参数。

## Wake：资讯驱动与持久化候选池

- 所有 alert / content 源先写入 `wake_proactive.db` 候选池，状态包括 unread、consumed、expired。身份由插件、源及事件 ID 组成，跨源同名事件不冲突。
- Content 写入本地成功后即可向源端 ACK；这表示候选已经交给本地管理，不表示已经发给用户。未选中的内容留在池中，重启仍可参与后续判断。
- Alert 优先，每轮自然化处理一条告警；只有成功发送后才消费告警并排队 ACK。ACK 失败留在持久化队列，下轮及重启后重试，不再次发送已消费的告警。
- 内容评分使用兴趣、新鲜度、发布时间可信度和来源多样性。新鲜度半衰期 36 小时；缺失发布时间的可信度为 0.03，同源连续内容递减。
- 只有新入池内容启动一次内容概率判断，旧池提高价值但不会每次轮询重新抽签。模型完成 skip 也进入唤醒抑制期。
- 普通模式每 300 秒检查；ReplayClock 模式使用可控时间与独立随机数。Default 的电量预设和每日配额不会套用到 Wake。

Wake 分两阶段处理内容：`scratchpad` 提交候选筛选，再并行调查选中候选的正文与偏好证据，最后使用 `share_content` 或 `skip_content`。模型使用本轮 candidate 别名，宿主验证别名并映射回候选池 ID 与来源证据。Alert / Context 使用 `send_event` / `skip_event`。

互动兴趣使用已提交普通用户与助手回合的向量原型。Kaze 用当前记忆引擎的同一 embedding 客户端，在 `sessions.db` 缓存完整互动的向量；每轮最多补 64 条，缓存按消息内容及模型校验。未完成互动、未来消息和主动消息不参与原型。未配置 embedding 时仍使用源评分。

Context 的 presence 变化、可打扰度变化和置信度决定是否重新评估，受全局三小时节流。状态变化不是直接发送指令。模型读取目标最近普通对话，以及整个工作区过去七天已成功发送的主动消息（最多 30 条、8000 字）；已聊过同一话题仍可根据新增事实继续跟进。

## Default：电量、配额与完整裁决

Default 运行 admission → source → route → prepare → judge → resolve → commit → schedule 阶段，三路数据经 DataGateway 聚合，资讯正文并行预取。专用工具包括 `get_alert_events`、`get_content_events`、`get_content`、`recall_memory`、分类工具、`message_push` 和 `finish_turn`。`message_push` 在这里提交发送提案，由宿主统一裁决与投递。

电量按 30 分钟、4 小时、48 小时三段衰减参与检查调度。它描述互动状态，和设备硬件电池没有关系。

| profile | 较短 / 较长检查 | 抖动 | 每日额度 | 最小间隔 |
| --- | --- | --- | --- | --- |
| daily | 4 / 8 分钟 | 20% | 48 | 180 秒 |
| quiet | 15 / 30 分钟 | 30% | 12 | 600 秒 |
| dev_verify | 30 / 60 秒 | 0 | 999 | 20 秒 |

`[proactive.anyaction]` 可覆盖 `enabled`、`daily_max_actions`、`min_interval_seconds`、`probability_min`、`probability_max`、`idle_scale_minutes`、`reset_hour_local` 和 `timezone`；兼容 Akashic 的 anyaction_ 前缀键。日常默认概率 0.20–0.82，北京时间 12:00 重置。关闭 enabled 后取消这一层限制，其他裁决仍生效。

投递键去重存放在 `proactive.db`；根据来源证据、引用 ID 或正文生成键，日常窗口为 10 小时。独立语义裁决比较最近真实发送的主动消息及状态摘要，模型必须返回有效 JSON。只有成功发送后才记投递、配额和 presence，失败不会进入成功历史。

## 源声明与共享 MCP

插件可通过 `agent.plugins.ProactiveSourceSpec` 声明 id、channels、server、fetch_tool、ack_tool 与 fetch_page_size，并通过 `McpServerSpec` 声明服务；`PluginManager` 收集到当前主动能力快照。每轮和长期 Kernel 的 lease 固定插件执行图和源声明，结束后释放。MCP 工具从宿主当前共享目录解析，新增、删除或重连服务后，主动 fetch、ACK、刷新和 Drift 无需重启即可使用更新后的连接。主动与普通 Agent 使用同一个 MCP 连接，同一 stdio RPC 串行执行，不再创建第二套主动连接池。

主动源配置或生命周期初始化失败时，应用记录异常并关闭本次运行的主动推送，普通聊天和调度服务继续运行。失败初始化取得的主动状态资源会被关闭。

Kaze 兼容工作区 `proactive_sources.json`，示例见 `config/examples/proactive_sources.example.json`：

```json
{
  "sources": [
    {"server": "news", "channel": "content", "get_tool": "updates", "ack_tool": "consume"},
    {"server": "sensor", "channel": "alert", "get_tool": "alerts", "ack_tool": "ack"},
    {"server": "device", "channel": "context", "get_tool": "get_context"}
  ]
}
```

服务器需已配置到同一工作区的 `mcp_servers.json` 或由插件声明。必须显式提供 fetch/get 工具名，没有固定 Feed 工具回退。JSON 源的相同路由合并读取，身份不受列表顺序影响；插件声明优先于相同 server / fetch 路由的 JSON 声明。

Content / Alert 返回列表，支持旧源的 items / events 包装与 get_args / ack_args；混合源提供 kind，单通道允许省略。Context 支持对象快照。正文读取与刷新职责分开：源拥有自己的刷新调度，主动 Kernel 读取缓存。旧 JSON 显式声明的 poll_tool / poll_args 由独立刷新任务调用，使用共享 MCP；feed.poll_interval_seconds 控制该任务间隔，停止循环时取消任务。

## 规则、插件与 Drift

每轮由宿主实际读取 `PROACTIVE_CONTEXT.md` 并注入；不存在时初始化规则模板，读取错误中止模型决策。可选 `HEARTBEAT.md` 作为附加任务规则，不再是启动前提。Kaze 的主动插件门控仍在取源前执行，activate 获得一轮完成回调。角色关系系统不属于这条链路。

Drift 使用完整的活动选择、执行、探索状态、记录和结束协议。活动来自 `workspace/drift/skills/<name>/SKILL.md` 与插件声明的 drift_skill_roots，可通过自带 create-drift-skill 创作。没有可运行的活动时不发送。Default 在无候选且冷却满足时进入 Drift；Wake 将抽样得到的 next_attempt_at 持久化，普通轮询不会重新抽样，用户活动及近期 Drift 会调整计时。

`drift.min_interval_hours` 是 Default 的最小 Drift 间隔；Wake 使用自身持久化计时算法。`adaptive_enabled`、`energy_contact_enabled` 和 `energy_contact_threshold` 仅为旧配置兼容保留，完整生命周期不执行旧简化入口的低电量空候选规则。

## 投递与诊断

用户在生成期间回复或目标处于普通回合忙碌状态时，宿主停止推送。正文和所有图片的渠道回执确认成功后，宿主一次性提交带 proactive、delivery_id、证据 ID 和来源引用的助手消息；图片同步服务将历史提交交给该宿主。失败的图片不会写入成功历史，普通推送工具也会返回真实的失败原因。桌面通知在历史提交之后发布，界面得到完整快照。

发送共用宿主 `ChatLane`，按渠道和规范传输目标协调：已准入的被动回合及其排队回复优先，主动与定时消息按进入发送窗口的顺序依次执行。窗口覆盖整条消息的正文、图片和成功历史提交，桌面覆盖到 `chat.done`；发送失败或取消会释放窗口。用户回合内的工具采用被动发送规则，避免等待自身或与其他回合相互等待。忙状态使用实际会话 key，包括后台调度关联的目标 key；桌面已经带前缀的 key 不再重复拼接。

定时任务的内部推理使用 `scheduler:{job.id}`，不读取或保存会话历史，也不触发普通互动提交和后台记忆整理。成功投递后才向任务保存的目标会话追加一次助手消息，失败和空响应不追加。发送确认成功后发生取消，宿主仍会完成历史提交再释放发送窗口。实现边界与独立并行评估见 [运行协调说明](../runtime-coordination.md)。

`proactive.db` 记录 Default 的 tick、步骤、去重键与投递状态；`wake_proactive.db` 记录候选池、ACK 队列、唤醒 / Context / Drift 状态及模型输入观察；`drift/drift.db` 保存完整探索状态。`sessions.db` 的主动助手消息是成功投递历史。决策表里 reply 表示模型作了发送提案，不能据此认定渠道已送达。

旧 proactive_deliveries / proactive_quota 分会话 JSON 文件仍保留。启动时仅将旧成功回执中正文与时间相符的现有助手消息识别为主动历史，不新建或删除消息，供新管线保持已发送上下文；新去重与唤醒使用完整生命周期的状态层。来源及适配说明见 [Akashic 主动链路来源](../third-party/akashic-proactive.md)。
