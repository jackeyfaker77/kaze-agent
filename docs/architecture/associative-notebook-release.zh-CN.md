# 联想记事本：默认关闭的最小 Host 接入

本次交付可由配置启用的原生插件，不切换 `memory.engine`，不迁移旧记忆，不新增轮询、发送队列、任务状态机或打扰策略。发布代码与启用生产分开：本 PR 默认关闭；开发验收只使用临时数据库和假模型、假发送边界。

## 按 Maka 的交付顺序接入

参考本地 Maka `1b75728483076c9b9c73c26602648bbecbe8585b` 的实际提交顺序：[原生服务 #6004](https://github.com/apache/maka/pull/6004) → [Inbox Bot #6124](https://github.com/apache/maka/pull/6124) → [发现工具 #6256](https://github.com/apache/maka/pull/6256) → [共享联想笔记和送达反馈 #6244](https://github.com/apache/maka/pull/6244) → [投递同步 #6278](https://github.com/apache/maka/pull/6278)。采用其先形成 Host 内可用的纵向功能、提交 PR、CI、审查后逐步启用的方式；不继续以离线报告代替功能交付。

数据库边界沿用 Kaze：对话材料来自 `SessionStore.committed_history_snapshot()` 回放 Runtime Event Log；`sessions.db` 的 UI 缓存不是事实源。笔记是单独的派生 SQLite 数据。送达证据来自 Kaze 已有 Outbox 的已确认分片，不能把 Maka 的数据库结构、source ACK 或“已生成”当成送达。

## 已接通的路径

1. 插件启用时，按 Runtime root + 明确的 `session_key` 建立独立笔记目录。首次记录启用边界，只处理之后的新用户材料，不批量回填历史或调用 embedding。
2. 现有主动 tick 拉取原生 content source，每轮最多探索 1 条未覆盖材料，积压材料按接纳顺序逐轮处理；有新材料才依次探索、判断，通常最多两次既有 light provider 调用，输出上限各 4096。保持当前 reasoning 配置，不因失败自动降档或提高上限。单条材料仍可能截断，分批只限制新增工作量；未全部覆盖前不消费旧认识。没有新材料不重跑判断。默认调用成本为零；启用后会产生当前模型费用。
3. 核心沿用已有 v3 来源覆盖、引用、事务和纠正规则。Host 外层记录原始响应、显式纠正目标和分片回执；不调整 v3 去迎合单个例子。取消一次活动只替换明确目标，保留无关认识。无法确认归属的回复不能用于纠正。
4. 仅最新一轮判断的有效候选进入现有 content 路径，不成为 alert，不绕过现有价值判断、电池、wake／drift、免打扰和发送队列。新材料尚未处理、来源被修改或删除、纠正或到期时拒绝消费旧候选，队列派发前再次校验。
5. 对话与 drift 可使用只读 `notebook_read`，与已有 `recall_memory` 并存；不改写现有记忆引擎、画像或历史。drift 读过笔记后将新鲜度检查组合到既有 Host guard，发送前认识变化则停止这次发送。
6. Telegram 回复单独保存 `user_text`；旧记录只有拼接引用且没有原文时不将其当作用户新陈述。回复归属同时核对会话、渠道、chat、当前账号和已确认 message id；聚合任务尚未写 done 也可使用已确认分片。

7. wake 的 Feed 内容初筛与最终判断复用一次 `notebook_read` 只读快照，包含有效笔记及回源证据，与已有 MEMORY 和最近对话并存；不增加模型阶段，也不把 Feed 正文写入笔记。沿用读取接口默认最多 10 条的有限视图，不宣称覆盖所有兴趣。笔记不是新闻事实或用户指令，一次提问不能直接推定为长期偏好。已就绪的读取会组合现有 Host 发送 guard，新材料、认识变化或过期导致快照失效时，即使待发消息只引用 Feed，也会在发送前停止；不绕过电池、wake／drift 或现有发送队列。插件关闭时不增加此段上下文；笔记尚未就绪或材料校验失败时返回空认识及状态，不为未使用的笔记安装发送 guard；会话不匹配仍拒绝访问。日历告警路径保持原状。

## 配置和启用

以下是示例，不能整份覆盖现有配置。`session_key` 必须填当前主动链实际使用的会话键，不能用显示名或另一个账号的键。

```toml
[plugins.associative_notebook]
enabled = false
session_key = ""
```

部署顺序：

1. PR 的后端和桌面 CI 通过，完成 review 后合并；首先部署默认关闭的版本。此时不创建笔记库，也不注册工具或来源。
2. 在有明确模型调用预算的启用窗口，备份当前配置及 workspace，填写目标 `session_key` 并设 `enabled = true`，按现有 Host 运维流程重载／重启。此次开发不执行该步骤。
3. 保留现有 `memory.engine`、本地／远程连接模式、主动调度和 300 秒轮询设置。不要另起 worker 或 cron。
4. 启用后新增普通对话材料，通过 `notebook_read` 查看 ready、笔记证据及 pending_runs；主动候选是否发送仍由现有链判断。使用一条明确纠正核对原目标退役、无关兴趣保留，并检查现有发送记录。真实模型语义效果和真实 Telegram 送达属于启用验收，不以假模型测试冒充。

数据位置：`<workspace>/associative_notebook/<session_key 的 SHA256>/`，内有身份标记 `sandbox.json` 和 `notebook.sqlite3`（沿用已验证的存储格式）。不要复制实验库、替换生产 sessions.db，或将笔记库提交到 Git。

## 失败处置和回退

- 请求派发前落盘 `awaiting_result`；未知、异常或截断不自动重发。已持久保存的响应可以在重启后本地校验提交；无响应的未决请求暂停记事本学习。`notebook_fetch` 返回 `learning=blocked` 和 `pending_runs`，不使可选学习失败升级为整个主动 tick 的未知副作用；原有 Feed 等来源仍可执行。持久化失败、执行权失效和取消不被吞掉。未就绪／校验失败的笔记不给出候选，发送前仍拒绝失效缓存。
- 完整原文保存在笔记库 `response` 事件。长度错误在 Runtime 模型错误事件和笔记库 `failure` 事件保留 `diagnostics.reason`（`input_limit`／`output_truncated`）及收到的截断响应，供私有诊断；它不是完整 response，不能提交覆盖或执行其中的工具。旧版本仅保存 `ContextLengthError` 的记录无法追溯区分两种原因，也无法恢复未记录的原文。
- 校验失败保留整份响应，不丢掉坏字段后继续。不要通过删库、改 runs 状态、删掉原响应或重新启用来重试未知请求。先在现有执行记录／模型服务记录中核对结果，再人工处理；本版不另造恢复控制台。
- 当前边界为 200 条有效笔记、单个请求 60000 字符；超过限制停止并明确报错，不静默裁剪或自动整理。这是首版规模上限，扩大前需另行实施容量策略。
- 回退只将插件 `enabled` 设为 `false`，按现有 Host 流程加载配置。笔记工具和来源消失；带校验要求的缓存候选因来源不再注册而不能发送。已经确认的分片只读恢复，不重发。保留笔记目录便于核查，不清空现有记忆或队列。
- 停用不会删除事实源或已送达消息。重新启用会继续原笔记库及其未决状态，不自动绕过异常。若需要回退代码，先停用插件并处理现有未决投递，再回到先前版本，防止老版本不认识新增的校验标记。

### 人工核对已记录的长度失败

只针对 Runtime Event Log 中可核实的、恰好一次物理尝试的 `ContextLengthError`。默认只预览：

```powershell
$env:PYTHONPATH = 'apps/backend'
.venv/Scripts/python.exe -m plugins.associative_notebook.recovery `
  --directory '<workspace>/associative_notebook/<scope-digest>' `
  --event-db '<workspace>/private_runtime/execution/events.sqlite3' `
  --turn-id '<failed-turn-id>' --run-id '<pending-run-id>' `
  --reason '<人工核对理由>'
```

核对输出的请求摘要、身份和调用编号，备份后停止 Host，再用相同参数加 `--apply`。命令复用 Host 工作区锁，运行中的 Host（即使状态文件缺失）会拒绝处置。它不调用模型、不写 coverage、不删除响应，只追加核对回执并将该 run 置为已有的 `discarded`；重复相同处置幂等，冲突回执拒绝。只有明确退役后，后续授权启用才可为未覆盖材料建立新的请求编号；这不是将未知请求改为成功或原编号重放。

核对从 Runtime 事件重建请求，必须与笔记请求逐项对应，且同 root、同 session、执行已结束、无发送事件、没有完整响应。超时、未知结果、多个尝试、身份或材料不匹配全部拒绝。生产路径应保留上述原生目录布局；副本验证必须指向独立复制的笔记目录。恢复命令不处理其他错误类别，不替代已有执行核对接口。

## 必要验收

自动化覆盖默认关闭零副作用、原生来源接线和作用域、事件材料权威性、双阶段增量、无新材料零调用、未知不重跑、无效响应保留、重启衔接、明确纠正不误伤兴趣、引用文本与用户原话区分、已确认分片归属，以及读取／发送前的新鲜度校验。额外覆盖真实 SDK 的本地模拟截断、Feed 故障隔离、后续 tick 零重试、长度失败核对与错误身份／请求／超时／运行中 Host 的拒绝。测试全部使用临时目录和本地替身。

发布门槛为仓库 CI 加 review，不再追加模型单例评估。上线后的模型效果、实际发送和长期容量仍是运行观察项；它们不能被本次离线通过证明。
