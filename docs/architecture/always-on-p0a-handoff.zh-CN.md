# Always-on P0a：子任务结果耐久交接与硬限额

日期：2026-11-01。源码起点：`be45a444c3b5fe16f196a694c444c22bc3624941`。本批仅修改当前共享 checkout，未切换分支、创建 worktree、启动生产 Host、连接 Telegram 或调用真实模型／外部服务。

## 实现边界

沿既有 `SubagentManager → ExecutionJournal → SpawnCompletionItem → MessageBus → process_spawn_completion_event` 增量修复，没有引入消息中间件。所有新持久表均位于既有 `private_runtime/execution/events.sqlite3`；用户通知继续复用现有 transport Outbox。

1. 后台派发前，Host 以当前 `ExecutionScope.turn_id` 预占资格并保存来源路由。新 job 使用完整 UUID；模型不能用 task、label、profile、session_key 或 retry_count 选择另一份父任务账目。
2. 子执行 `finish()` 在提交 `run.ended` 的**同一 SQLite 事务**中派生 completion intent。intent 写入失败会回滚终态；启动时还从已有终态幂等补齐缺失 intent。已完成子执行只补交结果，不重跑模型或工具。
3. MessageBus 轮询 completion intent，采用 epoch OS 租约及唯一 claim token 领取。活着的消费者没有超时抢占；死亡 epoch 可被接管。接管时同时恢复该 epoch 的未终结执行，因此已在运行的 peer journal 也可以安全接手。
4. 父消费执行以 `source=spawn_completion, session_key=原会话, request_key=child job_id` 接纳；执行事实和 consumer_turn_id 回执链接在同一事务提交。重复消息复用同一父执行。
5. 仅有接纳事实、**没有 run.started** 的父消费执行可写入 `spawn.admission.reclaimed`，以相同 turn_id、run_id 接续。run.started 之后被杀则保守停住，保留 interrupted / needs_review，不自动重进模型／插件／工具。
6. 父消费终态完成后、ACK 前退出，恢复读取已存回复，跳过模型与历史写入。ACK 使用原 claim token，重复 ACK 幂等；陈旧 token 无效。接纳冲突会释放 claim，不能误记 ACK。
7. 外部渠道在 ACK 前确保 `reply:spawn:<job_id>` 已进入既有 Outbox，并链接父消费 execution。已存在的回复（包括未知物理发送）不覆盖。消费回执不代表渠道已物理送达；实际投递仍由 Outbox 的既有安全策略负责。
8. needs_review 和 cancelled 结果走通知路径，不唤起父模型。未知副作用、未完成任务及原有受控恢复规则保持保守。
9. 复审修复：人工 close 的消费执行不允许 reclaimed，投影同时强制此规则；已完成但未消费的 job 被取消后，父消费只发送停止通知，原子执行结果保留。
10. 外部渠道在派发时保存 Host 提供的 `_spawn_account`，领取、父消费启动及回复入队／ACK 前均检查账号。账号变化、未就绪或旧 intent 缺少账号时保留 pending，不自动换绑。desktop／cli 区分 pipeline 已发布与包装器返回的停止通知／回放，只发布一次。

## 稳定身份与下游接口

`ExecutionJournal` 组合 `core.execution.spawn_handoff.SpawnHandoff`，对下游暴露以下接口：

| 接口／字段 | 契约 |
|---|---|
| `reserve_spawn(job_id, parent_turn_id, profile, route)` | 必须在派发前调用；原子占用 Host 额度，要求当前 epoch 的 running 父执行；返回实际 retry_count 与父身份 |
| `spawn_parent_context(turn_id)` | 沿消费、显式 retry／continuation 链解析稳定 parent_task_id；不会执行模型 |
| `spawn_task(job_id)` | 读取 parent_task_id、直接 parent_turn_id、profile、retry_count、cancelled 和原路由 |
| `spawn_completion(job_id)` | 读取不可替换的 payload、owner_epoch、claim_token、consumer_turn_id、acked |
| `claim_spawn_completion(accounts=...)` | Bus 提供当前账号映射，跳过账号不匹配的 intent；唯一领取一个未 ACK 结果，只有死 epoch 的领取可被接管；底层管理调用可省略账号过滤 |
| `admit_spawn_consumer(job_id, token, session_key)` | 校验领取和原会话，原子接纳／复用父消费执行，并链接 receipt |
| `run_spawn_recorded(journal, item, session_key, operation)` | 生产消费包装器；设置父 ExecutionScope，完成时保存回复；只回放回复，不重跑已开始的失败／未知工作 |
| `settle_spawn_completion(job_id, token, handled=True)` | 明确处理完后 ACK 终态；handled=False 时释放未接纳／终态领取，活跃执行保持保护；Bus 从包装器完成标记推导 handled |
| `cancel_spawn(job_id)` | 持久撤销同一父任务的后续派发资格；不回收已使用额度；返回是否找到持久任务 |
| `reconcile_spawn_completions()` | 从已提交子执行终态补齐 intent；不调用任何模型／工具 |

完成事件新增 `parent_task_id`、`parent_turn_id`、`child_turn_id`、`artifact_refs`。`artifact_refs` 当前提供该 job 的任务目录引用；它不证明目录内容存在、正确或通过验收，完整结果仍在子执行 `result` 中。Bus 只载入经过长度限制的摘要。

父身份含义：

- `parent_task_id` 当前锚定原始父执行（并沿现有显式 retry／continuation 解析祖先），不是新增 Goal ID。
- `parent_turn_id` 是本次实际发出子任务的执行；补做时它是前次结果的父消费执行。
- `child_turn_id` 是执行日志中的子执行；`job_id` 是业务委派身份，两者分开。
- `consumer_turn_id` 是本次子结果对应的父接续执行，与原始父执行分开。

P0b 可以按这些身份建立 Goal 关联，保持完成 intent、消费身份、额度和回执契约。P1 可复用完成结果作为来源版本，不能把 ACK 擅自解释为 Goal 验收通过，也不应另建同类 completion 队列。

## Host 硬限制

本批采取保守的**每个稳定父任务最多两次后台派发预占（初次 + 一次补做）**。同一父执行主动拆成多个后台分支也共享这两个名额，不能借“新 task／新 label”增加额度；不是每个参数组合各两次。已有并发门控仍然有效。

- retry_count 兼容参数保留，但后台实际值由持久预占顺序生成（0、1）；模型报 999 或重置为 0 均不改变账目。
- 父消费执行只有收到 max_iterations 或 tool_loop 结果才允许自动补做；completed、error、cancelled、needs_review 不取得这项资格。
- 补做 profile 不能扩大初次工具权限。general 可缩为 research 或 scripting；research／scripting 不能互换或升为 general。
- 补做改为同步 spawn 仍占同一份持久额度；普通对话的既有同步路径保持原行为。
- 取消后同父任务不得再派发；晚到结果不能重置取消记录。旧 job 在重启后没有内存 Task，仍可持久撤销后续资格。
- 预占后进程退出也不退款，不推测没有发生派发。本批不是全目标 token／费用账本，也不改变现有模型迭代上限。

## 故障恢复语义

| 故障点 | 恢复行为 |
|---|---|
| 子终态与 intent 事务提交前 | 原子回滚；未知／未完成前缀按既有执行恢复，不自动重跑 |
| 子终态后、发布前 | 直接领取已提交 intent；子模型及外部动作均不重做 |
| 父接纳后、run.started 前 | 接管原 consumer_turn_id；没有执行任何模型／插件／工具，允许安全首次开始 |
| 父已经开始后、终态前 | 保留原执行事实、发送待核对通知；不自动重复模型或外部动作 |
| 父终态后、ACK 前 | 回放 journal.result，补齐稳定 Outbox intent，ACK；不再次消费模型结果或追加同一历史 |
| Outbox intent 后、ACK 前 | 复用原回复身份；不覆盖未知发送，不新增第二次物理发送资格 |

ACK 意为“该完成结果已被父执行处理，或已明确转为待核对终态”。started 后崩溃可能需要人工处理，这不宣称任意模型／外部服务具备 exactly-once 恢复能力。

## 验证入口

新增离线测试：`tests/backend/test_spawn_handoff.py`。测试仅使用 pytest tmp_path、真实 SQLite、假 provider／子模型、隔离文件动作和子进程 `os._exit(71)`；不读取生产 workspace，不连接网络。

```powershell
.venv\Scripts\python.exe -m pytest tests/backend/test_spawn_handoff.py -q
```

覆盖：终态与 intent 原子回滚、投影重建保留回执、并发唯一领取与活 epoch 保护、陈旧 token、接纳冲突释放、同运行期 peer 接管、父链配额与同步绕过、实际后台补做 Host 计数、取消先于 coroutine 开始、真实 AgentLoop 假 provider 消费与回复回放、安全通知零模型调用。

真实子进程故障点包括：子终态后、发布前、父接纳后、ACK 前、父动作完成后、子动作结果未知时。通过 fsync 的文件计数核对父／子模型及动作次数；未知子动作恢复不启动父模型。具体测试命令和最终结果见下方交付验证记录。

## 残余限制与回退

- 没有 Goal、目标验收器、通用事件订阅、外部 Codex 执行器或统一费用预算。
- 旧版本未保存路由的历史 subagent 执行不会猜测接收人；只对本批预占时保存路由的任务保证自动补交。
- 预占后、子执行尚未接纳的窗口可能只留下资格记录；不自动启动未知任务，额度不退。已开始的父消费没有自动续跑能力，按受控核对处理。
- 依赖同机 OS 租约及 SQLite，未扩展为多机任务系统。
- 新 coordination 表保存预占／取消／ACK 权威状态；投影重建不会清空它们。备份和恢复必须保留整个 execution SQLite，不能只复制 events 表。
- 外部回复通过既有 Outbox 保证耐久意图及未知发送保护；desktop／cli 维持本地输出语义，不能承诺崩溃后的 UI 物理显示恰好一次。
- schema 为新增表，旧数据保留；新 `spawn.admission.reclaimed` 事实需新代码读取。回退前必须停止相关消费，保留数据库；不能让旧二进制继续消费已经出现的新事实。

## 共享文件边界

本批修改：`agent/background/{runtime,subagent_manager}.py`、`agent/looping/{handlers,core/processing}.py`、`agent/tools/spawn.py`、`bus/internal_events.py`、`bus/queue.py`、`core/channels/hub.py`、`core/execution/{journal,projection,runner}.py`；新增 `core/execution/spawn_handoff.py`、`tests/backend/test_spawn_handoff.py`、`tests/backend/test_spawn_handoff_review.py` 和本文档。

共享 checkout 中原 `_ensure_execution_notice` 逻辑、hub 的原 `execution_notice` 分支和下游 `journal.goal.state` 兼容分支保留，但从本批独立提交中排除。hub 本批只增加独立的 `spawn_completion_notice` 分支，防止停止通知错误更新其他助手消息的送达状态。其他既有路线、notebook、experiments 和下游 Goal／scheduler／bootstrap 改动不进入本批。用户授权提交合并后，从干净 HEAD 导出普通目录验证本批精确内容，再用临时索引组装提交；不切换共享分支、不创建 worktree。

## 交付验证记录

- 最终扩展回归：**140 passed，238.74 秒**。入口如下，覆盖 execution journal／projection／真实进程恢复、受控续跑、durable transport、原执行停止通知及 spawn 相关路径。

```powershell
.venv\Scripts\python.exe -m pytest tests/backend/test_spawn_handoff.py tests/backend/test_execution_journal.py tests/backend/test_execution_projection.py tests/backend/test_execution_process_recovery.py tests/backend/test_continuation.py tests/backend/test_continuation_process.py tests/backend/test_durable_transport.py tests/backend/test_execution_stop_notice.py tests/backend/test_spawn_completion_flow.py tests/backend/agent/background/test_subagent_manager.py tests/backend/agent/tools/test_spawn.py tests/backend/agent/tools/test_spawn_tool_call.py -q
```

- 最后一次交接专项回归：**17 passed，32.25 秒**。包含新增的重启后硬限额检查，以及消费包装器直接调用时稳定 Outbox 身份的验证。此数字与扩展回归重叠，不相加。
- `ruff check` 本批全部 Python 改动及新测试：**All checks passed**。
- `git diff --check`：通过；HEAD 仍为起点提交，未暂存或提交共享工作树。
- 全部故障注入在临时目录及测试子进程中完成；没有生产 Host／Telegram／真实模型服务验收，本批不声称已经部署。

### 独立复审后的提交验证

- `tests/backend/test_spawn_handoff_review.py`：16 项通过，包含人工 close 防重开、取消完成结果零模型调用、账号变化的领取／接纳／入队／ACK 窗口，以及 desktop／cli 的正常、取消、关闭、回放单次通知。
- 独立提交快照的生产和测试 Pyright 均为 0 errors；Ruff 与 diff 空白检查通过。
- 上述历史共享树测试命令包含未提交的停止通知测试；独立提交只包含本批两个新测试文件，不依赖这些既有未提交内容。独立快照的扩展回归与 PR CI 结果以交付报告和关联 PR 为准。
