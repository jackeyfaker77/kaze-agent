# Phase B：主动链受控恢复契约与证明矩阵

状态：实现完成，最终验收结果见本文末尾；代码基线 `d6db2e7`，本轮工作分支 `codex/proactive-controlled-recovery`。下表保留实施前的证据核对，不把原有事件化测试误算为受控恢复证明。

## 权威与关联

- Runtime Event Log 拥有候选接纳、判断请求与返回、决策、恢复计划依据、恢复执行关联。
- Outbox 拥有物理发送及分片回执；执行日志中的未知 delivery 不覆盖队列中已提交的真实回执，人工结论也不伪造成回执。
- Wake reservoir / 主动状态库拥有消费、ACK 待办和打扰状态。恢复必须使用显式数据协议，不能反序列化或重跑任意回调。
- 待办取消和版本由来源 validator 裁决。查看计划不调用 validator；确认时在发送通道锁内重新检查。不可用时阻塞。
- 同一来源边界只有一个持久 continuation claim；原执行保持原终态，新执行记录实际补处理。重建不取得执行资格。

## 基线证据及缺口

| 边界 | 已有持久证据 | 本阶段恢复规则／需要补齐的证明 |
|---|---|---|
| 接纳后、尚未获得可恢复输入 | request.accepted、候选 claim | 不凭当前环境重造旧输入；没有版本化安全边界则阻塞 |
| 输入已冻结、模型尚未派发 | Wake observation 有输入，执行事件目前只有摘要 | 补交互日志中的恢复边界；确认后重新核对资格，只执行尚未派发的判断 |
| 模型派发、返回未提交 | proactive.model.requested、model.request.prepared | 保持未知，不能把重启当作重试许可 |
| 模型返回已提交、决策尚未提交 | proactive.model.returned / model.response.recorded | 从原结果解析决策，不再调用模型；缺少解析边界的旧历史阻塞 |
| 决策已提交、未入队 | resolve、delivery.intent；原回调不能重建 | 使用版本化恢复材料；保留原候选及同一 delivery ID；确认前再验证 |
| 入队后、物理派发前 | jobs + execution links | 重启 worker 不得绕过确认、取消、过期和用户活动检查 |
| 物理派发后、回执前 | parts.state=sending | needs_review，重复确认、重放均不能再次发送 |
| 分片回执提交、jobs.done 前 | parts.done + message_id | 返回已存分片结果；未完成分片仍需发送前校验，不推断整条已送达 |
| jobs.done 后、会话／领域收尾前 | queue result、delivery_id、conversation facts | 只补幂等本地状态；不能再发送或重做已完成判断 |
| 来源 ACK 结果未知 | tool.dispatched 未确认 | 保持未知；不以用户确认伪造成功，不盲目重跑外部 ACK |
| 恢复 claim 后再次崩溃 | continuation.started + 唯一索引 | 同一来源不能再创建第二个恢复执行；后续从新执行的事实判断 |
| 用户结束处置／队列关闭 | execution.reviewed / queue review_closed | 持久停止；不能因重启或新 action ID 绕过 |

当前基线测试 `test_proactive_event_recovery.py` 覆盖八个真实退出窗口，证明“不重新运行旧 tick”和分片去重；它没有证明恢复前重新验证。其入队后自动恢复行为需要随新契约修订。

## 本阶段测试要求

- 真实子进程在输入冻结、模型返回、决策、入队、派发、回执、领域提交和恢复领取后退出；重新开库。
- 对已提交判断验证模型计数不增加；对已提交回执验证发送计数不增加；未知模型／发送结果保持阻塞。
- 反复重建执行和会话投影不写新事件、不产生外部调用。
- 计划只读；旧计划失效；重复点击与两个客户端竞争只产生一个后继执行。
- 当前用户活动、关闭、禁用、过期、来源移除、待办取消／版本变化和 validator 不可用均阻止新发送。
- 本地与鉴权远程 RPC 共用 Host 服务；300 秒轮询和现有配置保持不变。

## 范围边界

只恢复有明确持久输入和收尾协议的安全边界。任意 Drift 工具程序、旧历史缺失材料、未知外部工具结果不推测续跑。Markdown 历史归档与近期摘要持久恢复仍未完成，另轮处理。本轮不切换记忆引擎、不部署服务器。

## 实现后的调用链

1. 正常 Wake 单事件（alert / context）在模型派发前提交 `recovery.boundary`。其中包含原候选、原请求、原 Wake 时间、稳定投递身份，以及用户活动／配置／来源指纹；生产模型调用消费该冻结请求。
2. `execution.plan` 只读取事件和领域回执；`execution.continue` 再核对计划，并在同一日志事务内提交新执行及唯一 `continuation.started`。本地工作台与鉴权远程客户端共用这个 Host 服务。
3. 已提交的模型返回直接解析成原判断。模型正式返回已落盘、外围主动审计尚未落盘的窄窗口，也用同一调用的正式返回恢复。模型结果未知时阻塞。
4. 新发送在通道锁内核对当前来源版本／取消／有效期；Context 重新读取同一来源并比较语义。异步检查返回后再次核对用户活动。关闭、禁用及配置变化不会偷偷转成重新判断。
5. Outbox 保存来源执行及完整 Telegram 分片清单。普通后台 worker 不再自动发送遗留主动任务；确认后的执行复用原 job 和分片身份。已完成分片直接返回回执，尚未派发的分片必须重新验证。`sending` 保持未知。
6. 所有分片都有回执时，允许只补会话及领域收尾，即使提醒现在已经过期；此模式禁止任何缺少回执的新分片。已经提交 `recovery.settled` 时只补执行终态。
7. Wake 的候选消费、ACK 待办与 `event_finalizations` 收条同事务提交。ACK 派发前持久标记 `sending`，成功后保留 `done`，重复消费不会重新创建已完成 ACK。旧版无法区分是否已派发的待办迁移为未知。没有核验协议的未知 ACK 不自动重试。
8. “结束处置”是持久停止，不再释放同一候选供下轮重发；新的候选版本仍可被接纳。旧版本已经提交的重新接纳事实仍可重建，历史不改写。

源码入口：

- `core/execution/proactive_recovery.py`：只读事实解释、计划及原子领取。
- `desktop_bridge/proactive_recovery_service.py`：Host 验证与受控执行。
- `plugins/wake_proactive/runtime.py` / `state.py`：单事件边界与领域收尾协议。
- `bus/durable.py` / `infra/channels/telegram_channel/durable.py`：发送保护与分片清单。
- 工作台复用现有恢复入口；展示“首次判断／复用判断／只补回执／只补终态”的区别。

## 崩溃证明矩阵与测试入口

`tests/e2e/proactive_event_crash.py` 使用真实 AppRuntime、Kernel、SQLite、会话存储和 Outbox，仅替换模型及物理渠道为 fsync 计数器；故障通过 `os._exit(73)` 注入。每个窗口重新开库，并重复恢复、重建执行与会话投影。

| 测试窗口 | 预期结果 |
|---|---|
| before_model | 确认后第一次判断、一次发送 |
| model_unknown | 模型计数停在 1，发送 0，保持未知 |
| canonical_response / after_model / after_decision | 复用已提交返回，模型总计 1 |
| after_enqueue | 未确认不发送；确认后同一 job 发送一次 |
| physical_unknown | 已派发计数 1，永久保留待核对，不补发 |
| after_receipt / after_queue_done | 复用回执，发送总计 1，会话一条 |
| after_finalization | 复用领域收条，仅发送尚未派发的来源 ACK |
| ack_unknown | 发送一次、ACK 派发一次，未知 ACK 保持阻塞 |
| after_ack | 复用 ACK 的完成状态，不再次调用来源 |
| after_settled | 仅补执行终态，无模型／工具／发送 |
| after_completed | 查看／重复重放不产生新工作 |
| continuation claim 后再次退出 | 从关联执行恢复，原边界唯一 claim 不被绕过 |

补充测试：

- `test_remote_proactive_recovery.py`：两个真实鉴权 WebSocket 客户端竞争、重复确认、计划不改数据库；取消、过期、禁用、关闭、用户活动及验证期间的新消息。
- `test_proactive_recovery_contract.py`：真实 Telegram 渲染后的多分片恢复、发送前再次拦截、旧主动队列不能绕过确认、领域收条事务回滚和 ACK 去重。
- `test_alert_validation.py`：恢复前重新读取 Context；当前语义变化阻止投递。
- `test_proactive_event_recovery.py`：原八个窗口继续证明“重放本身不执行”。

明确限制：本轮适配 Wake 的单条提醒／Context 决策及其收尾。内容调查和完整 Drift 在任意中间工具点的续跑、带自定义插件 gate activation 的收尾，没有对应协议时继续阻塞。不能借已有 `resolve` 文本重造任意 Python 回调，也不将这些边界声明为可自动恢复。

## 验收记录

- 平台：Windows，本地隔离工作区；远程模式使用本机鉴权 WebSocket Host。
- 后端全量运行：`2201 passed, 1 skipped`（857.03 秒）。全量后补充的 5 个边界场景通过专项复测：恢复／远程专项 `22 passed`，Context／验证边界专项 `33 passed`，收发队列／人工处置专项 `29 passed`；这些专项存在重叠，不将其简单相加冒充独立用例数。
- 15 项真实退出／恢复测试全部通过（14 个窗口及一次领取后再次退出）；旧八窗口重放测试保留。每个恢复场景重复开库，已提交模型、已确认发送、来源 ACK 的计数不增加。
- 后端源码与测试 Pyright 均为 `0 errors, 0 warnings`。桌面 typecheck、lint、生产构建通过，单元测试 `224 passed, 1 skipped`。
- 全量验收发现并修复了计划入口推进执行投影 checkpoint 的只读回归；修改后数据库 dump 不变。停止、未知 ACK 以及主动任务的发送授权测试按新契约更新。
- 未重启用户正在使用的 Kaze，未进行本轮真实 Telegram 网络发送；上述物理计数测试与既往真实 Telegram 验收分别看待。
- 未修改生产配置或 300 秒轮询；未实施 Markdown 归档／摘要恢复、Akasha 迁移、服务器部署或整包备份恢复演练。
