# 事件事实 → 可重建执行状态与人工处置

后续增量：[收发队列关联查看与人工处置](transport-review-v1.md) 已补上独立队列入口，
并在申请整轮重试时校验关联发件。下文保留执行投影阶段的实现记录。

本轮基线为 master `6df1a67495b92511e8a39edb7346bf5404a9b7d8`，已实时核对
GitHub master；开始时工作区干净。此前代码已具备执行事实、会话投影、独立 Host、
远程桌面与持久收发队列，本轮在这些入口上增量实现，不另建运行时或事件库。

## 事实与投影

`private_runtime/execution/events.sqlite3` 的追加写事件继续作为事实源。
`core/execution/projection.py` 是纯状态转移：输入事件前缀决定执行身份、状态、
结果、工具/发送操作、人工结论和重试关联。它不读取时钟，不调用模型、工具、
发送器或会话写入接口。未知版本、未知执行事件、缺失接纳事实、身份不匹配、
重复派发/返回、非法终态转换和不一致重试关联会失败，不能降级成空状态后重跑。

`executions` 保留既有查询/唯一接纳约束；新增 `execution_views` 保存详细投影，
`execution_checkpoint` 保存全局重放位置。状态变更由先写入的事实投影产生，
事实与视图在同一个 SQLite 事务内提交。会话事件仍交给既有会话投影处理。
分页 cursor 使用接纳事件 seq，重建后不会改变分页身份。

启动先在排他写事务内从全部事实重建执行视图，再检查旧 epoch 的 OS 锁并恢复
遗留执行。后续访问补齐其他连接追加的事件。重建中途退出会回滚视图替换，
再次启动仍可重做。重复重建不追加事实、不触发外部操作；重复恢复不追加第二条
`run.recovered`。既有 v1 事件可直接读取，不从聊天正文推断工具安全性。

本阶段启动做完整重放，运行中增量重放；尚未引入快照压缩。大型事件库的启动耗时
和完整事件/附件保留仍需管理。完整性检查不是对恶意篡改数据库的密码学证明。

## 查看与处置

本地和已认证远程桌面共用「工作台 → 执行记录」：支持全部会话/指定会话、
状态筛选、分页、原始输入、保存结果、工具参数与返回、事件明细、原执行/重试跳转。
失败、已中断、待核对分别显示 `failed`、`interrupted`、`needs_review`。
即使没有可见会话消息，仍能在全部执行中找到已接纳记录。

新增协议：

- `executions.list`：`session_key` 可省略，新增 `status`（单一状态或 `attention`）。
- `execution.get`：返回 `last_seq`、`effects`、`retry`、`review`、重试双向关联。
- `execution.review`：`turn_id`、`expected_seq`、`action_id`、`decision`、`note`。
- `execution.retry`：`turn_id`、`expected_seq`、`action_id`；输入由服务端读取原事实。

处置带当前事件位置，过期界面提交被拒绝。`action_id` 与 RPC id 分开；同一操作
重发返回已有结果，用同一标识改结论则冲突。处置事实追加为 `execution.reviewed`：

- `close`：结束处置，保留原终态、输入和回执，不执行任何动作。
- `no_effect`：操作者确认所有需核对的外部操作均未产生副作用，并记录核对依据。
  必须先确认原执行者已停止、检查目标系统与相关投递队列，再作此判断；不确定时
  保持待核对或结束处置。已有确认返回的外部操作不能被改判为未发生。

人工结论和物理回执分开保存：`no_effect` 不会把未知回执改成成功，也不会把
原 `needs_review` 改成 completed。界面同时展示历史终态和处置情况。

## 重试边界

| 原执行事实 | 整轮重试 |
| --- | --- |
| 无工具/发送派发 | 可显式重试 |
| 仅明确声明 `read_only` 的工具 | 可显式重试，未知返回不代表外部写入 |
| 未分类、Shell、写文件、MCP 或发送操作 | 默认禁止直接重试 |
| 外部操作已有确认返回 | 禁止整轮重跑；核对后结束处置，剩余工作另行提出 |
| 外部操作结果未知 | 保留待核对；全部人工核实未发生并保存依据后，才可显式重试 |
| 已有重试关联/已关闭/非异常终态 | 不再从该原执行创建另一尝试 |

工具安全性来自实际实现的 `Tool.retry_policy`，在调用前写进派发事实；不采用模型
参数、工具名称猜测或 MCP 提示注解作为安全凭据。目前只将本地 ReadFileTool、
ListDirTool 标为 read_only。旧事件缺少该字段时保守处理，不追溯升级为安全。
发送适配器明确拒绝接纳（已有的 `confirmed + ok=false`）与未知发送结果分开处理。

未知外部工具异常会沿 ToolRegistry、ToolExecutor、被动管线传播
`ExecutionNeedsReview`，立即停止该轮模型循环，避免模型把未知结果当普通错误立即
再次调用。同 scope 的后续工具也被阻止。只读工具异常仍可按原错误路径处理。

工具返回字符串不等于副作用已确认。实现通过 `Tool.execution_certainty` 解释自己的
返回协议：前台 Shell 的 `interrupted=true`、后台 `task_output` 的超时或丢失状态均
记为 unknown 并停止模型循环。后台启动的确认只代表启动成功，保守地禁止整轮重跑。
Shell 输出泵在超时取消时回收读取任务与进程管道，避免遗留 Windows 句柄。

嵌套派发保存 `parent_effect_id`。外层工具正常返回时，若其后代仍有未知外部结果，
外层返回也记为 unknown，避免 MessagePush 的失败文本被误判为发送已确认。
人工核对仍不能覆盖任何已确认成功的物理发送；部分成功的整轮只能结束处置。
缺少父关联的旧事实不推测嵌套关系，已有 confirmed 回执不被改写，继续保守处置。

同步 spawn 的子工具也进入同一执行 scope，按父子关系记录工具派发与返回。子任务
结果未知，或已有非只读操作但未完成时，安全停止向父执行传播，不能由父模型把整项
子任务重跑。后台 spawn 脱离父 scope，使用 `subagent:<job_id>` 的独立执行记录，
父会话结束后仍持续记录子工具事实；这些来源不开放桌面整轮重试。后台取消等待任务
与 Shell 输出泵完成清理，再依据最终执行事实发布通知。待核对通知直接告知用户，
不进入主模型工具循环，以免通知处理再次派发同一任务。

桌面与远程桥接返回 `execution_needs_review` / `execution_unavailable`，携带原
`turn_id`、状态与执行请求标识；日志写入失败返回 `execution_outcome_unknown`。
聊天界面对这些错误保留核对提示，不恢复已提交的输入和附件供直接重发。普通发送
携带显式执行请求标识；后端在接纳事务内还拦截同一会话、同一来源、完全相同原输入
对应的未处置异常执行，即使调用方换了请求标识也不能重复未知或已确认的外部操作。
此约束覆盖 failed、interrupted、needs_review 中不安全的效果；纯只读失败、不同
输入与成功完成后的正常新请求不做内容去重。人工 no_effect 后仍需从关联重试入口
发起；close 表示用户已结束处置，之后的新请求按新的明确意图接纳。

若执行已完成但 `chat.done` 或回复流失败，桥接返回携带原执行身份的
`execution_unavailable`；本地桥接退出、超时和写入失败也按结果未知处理。界面不恢复
已提交输入，并为尚未确认的相同输入保留请求标识；重新提交时读取该请求的已保存
结果，不新建执行。明确的安全失败释放该标识，保留原有恢复草稿流程。

`no_effect` 许可尚未被重试接纳时，可追加新的 `execution.reviewed(close)` 撤销。
原核对依据、撤销依据和原始回执全部保留，当前投影显示最后结论。撤销和接纳重试
共享事务与 `expected_seq` 检查，并发时只允许一方成功；已接纳的尝试须查看或停止
关联执行，不能通过改写原结论撤回已发生的执行。

重试是新 turn/run，用 `retry_of`、`retry_turn_id` 关联；沿用 v1 的一 turn 一 run。
`execution.retried` 与新 `request.accepted` 原子提交，进入现有会话锁和接纳唯一约束，
再开始执行。原终态不覆写。接纳后崩溃不会自动启动该尝试；重复请求不会重新跑模型。
完成结果可重复读取；其他状态要查看新尝试，不能盲目重发。

这是按原输入、当前配置和当前会话上下文发起新尝试，不是从模型 token 或某个工具
中间点继续。历史消息保留；重复事件重放不追加消息，明确的新尝试会产生自己的历史。
工具副作用的安全性仍依赖实现声明和人工核对的真实性，不承诺外部系统 exactly-once。

本轮桌面重试仅支持 desktop 来源。其他来源可查看并记录处置，原 inbox/outbox
继续拥有投递、分段和账户身份，桌面不另造一次 Telegram/主动发送。执行记录的关闭
不会修改队列状态；独立队列的人工处置与自动工具级续跑仍是后续工作。

## 离线重建

停止使用同一工作区的后端后运行：

```powershell
.venv/Scripts/python.exe apps/backend/main.py execution-rebuild --workspace ./workspace
```

命令取得既有 WorkspaceOwner 锁，后端存活时退出码为 2。不加载配置、模型或渠道；
重建视图并对已失去 epoch 所有者的执行追加保守恢复事实。它要求完整事件库已存在。
不要删除事件表来“修复”状态，也不要用不认识新处置事件的旧版本写同一工作区。

## Maka 参考与本轮范围

参考本机 Maka `59f61cf7d`：

- [Log is the runtime](https://github.com/apache/maka/blob/main/docs/blogs/log-is-the-runtime.zh-CN.md)
  与 `packages/runtime/src/runtime-event-read-model.ts`：从事实分类终态，拒绝不完整身份。
- `packages/core/src/tool-recovery-fact.ts`：核对观察、处置与原回执分开记录；本轮采用
  简化的人工依据，不照搬工具恢复协议、摘要证明或续跑调度。
- [Runtime Host](https://github.com/apache/maka/blob/main/docs/blogs/runtime-host.zh-CN.md)：
  继续沿用 Kaze Host 的生命周期和工作区所有权，桥接层只提供查询和处置入口。
- [Peer Mesh](https://github.com/apache/maka/blob/main/docs/blogs/peer-mesh.zh-CN.md)：
  仅作为后续背景。本轮不增加多节点、配置项、服务安装或服务器部署。

回归覆盖 `test_execution_projection.py`、`test_execution_disposition.py`、
`test_remote_execution_disposition.py` 和既有 execution/session/transport/Host 测试。
`test_execution_review_regressions.py` 以真实前后台 Shell 写计数器后超时、真实
MessagePush 嵌套发送以及成功写入后模型失败覆盖 review 反例，并验证重复重建不增效。
`test_execution_followup_regressions.py` 覆盖真实同步/后台 spawn 的超时、成功写入后
模型失败、后台取消、完成回复丢失后重复读取，以及许可撤销和重试的并发竞争。
`execution_projection_crash.py` 使用真实 `os._exit(73)` 覆盖重试提交前、接纳后、
外部副作用后、完成后、视图重建中、人工结论提交后，以及撤销许可提交前/后退出。
撤销未提交时必须回滚到此前结论；已提交时，重启和旧 action 重放都不能恢复重试许可。
`test_execution_process_recovery.py` 驱动 `execution_recovery_crash.py`，把后台写入后
硬退出、主执行完成但回复返回前硬退出固化为默认测试套件的一部分。每个场景启动
两个新进程恢复，并反复重建、读取原请求；断言模型与工具调用为零、副作用仅一次、
用户消息与最终回复各一条、消息 ID 不重复、执行状态和事件数量稳定。
`execution_workbench.cjs` 使用隐藏 Electron、真实 preload/RPC/Python 后端和临时工作区，
验证禁用危险重试、键盘输入核对依据、保存处置、安全重试、事件分页和窄窗口布局。
