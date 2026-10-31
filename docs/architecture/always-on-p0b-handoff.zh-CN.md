# Always-on P0b：持久 Goal、独立验收与跨执行预算

日期：2026-11-01。实现基线 `be45a444c3b5fe16f196a694c444c22bc3624941` 加共享工作树中的 P0a；先读取并等待 P0a 实际交接与验证，再接入。未切换分支、创建 worktree、重启生产 Host、调用真实模型／渠道或发布代码。当前为本地实现和离线验收。

## 实现与生产入口

新增 `apps/backend/core/goals/`，复用 `ExecutionJournal`、P0a 的事务内 `_admit`、`ExecutionScope`、已有 `SubAgent` 以及 `SchedulerService`。没有第二个 cron、消息队列、目标摘要库或记忆引擎。

`bootstrap.tools.build_core_runtime()` 创建 `CoreRuntime.goal_service`，绑定 `scheduler.bind_internal_tick("goals", service.tick)`。内部 tick 与普通定时任务共用已有时钟与任务关闭机制；Goal 子任务不走用户通知渠道，不会自动生成普通聊天。启动时没有显式 Goal 时不会请求目标模型。此接线需下次用户正常启动 Host 才加载，本批没有操作当前生产进程。

P0 Goal 采用一个固定文件工具 SubAgent 执行路径：只读显式声明的来源文件，读写本 Goal 的 `workspace/goal-runs/<goal_id>/artifacts/`；没有 shell、网络、spawn、message_push、记忆或授权管理工具。它复用 SubAgent 执行机制，但不冒充通用 `SubagentManager` 的 conversation_spawn 消费，也不修改 P0a 的父任务两次派发限制。目标自己按持久 `max_attempts` 和总预算限制修订。

## 权威事实、身份与状态机

权威记录为同一个 `private_runtime/execution/events.sqlite3` 中不可更新／删除的 `goal.state` 事件，领域 `schema_version=1`。`goal_views`、`goal_checkpoint` 只是可重建投影。ExecutionJournal 明确跳过这一独立领域的事件；普通执行／会话事实仍由原投影处理。

每次状态修改都保存完整目标快照及原因：

- `goal_id`、递增的 `version`；`definition_version` 与保留的 `definitions`。
- `definition`：目标、固定验收条件、受信任输入目录、来源路径、产物路径、子模型迭代上限。
- `authorization`：来源、允许动作、总额度及分类额度、最大 attempt、可选到期时间；`authorization_version`、`cancel_version`。
- `status`、`step`、`wait`、`observed_sources`、`reason`、`block`。
- `parent_execution_id`（可选原执行）、`attempts[].execution_ids`、`phases[].turn_id/phase_id`、结果中的 `child_job_id/artifact_refs`。
- 预占／结算回执、每轮验收证据、输入版本与产物 SHA256。

`version` 包括预算和回执变化；它不是只在计划变化时递增的版本。推进、修改定义、等待、取消均使用预期版本，旧版本返回 `GoalConflict`。重复创建相同初始请求幂等，不同内容不能复用 Goal ID。

```text
ready(execute) → running → ready(verify)
ready(verify)  → running → succeeded                       全部固定检查通过且版本未变
                         → ready(replan) → ready(execute)  失败且仍有修订 attempt
                         → ready(verify)                  检查期间输入变化，重新检查
                         → failed                         attempt 或预算耗尽
ready → waiting → ready                                   指定时间或来源版本变化
任意未撤销状态 → cancelled                                Host 取消、撤销或授权到期
未知执行／调用、无效输入 → needs_review                     不自动重发未知动作
```

`succeeded` 表示特定定义和输入版本已通过验收，不是永远有效的声明。调度器监测声明的来源及产物哈希；变化使成功目标重新进入 `ready(verify)`，旧证据保留。取消／失败／待核对状态不会被来源事件自动重开。

阶段领取与 execution 接纳／开始在同一 SQLite 事务中提交；阶段终态、阶段结果及下一 Goal 状态也同事务提交。全库同时至多一个 active Goal phase；不同 journal 实例并发领取也不能派出第二次执行。每个 attempt 可以依次有执行、验证、修订阶段，不支持 DAG 或多 Agent 协商。

执行 `completed` 只会进入 `ready(verify)`。执行者文本中的“完成”、`claim` 或 JSON 中的 `passed` 均不能代替 Host 的验收路径。仅 `GoalService` 的验证阶段汇总固定 criterion 的真实结果后才能成功。

## 验收和来源约定

创建时至少指定一个可执行 criterion；不提供纯 LLM 验收器。

| kind | 固定条件 | 持久证据 |
|---|---|---|
| `file` | artifact `path` 存在；可给期望 `sha256` | 绝对路径、SHA256、通过／失败 |
| `json` | UTF-8 JSON 对象、`required` 字段、可选 `equals` 精确 JSON 值 | 格式结果、文件 SHA256、错误类型 |
| `command` | Host 固定的 `argv`，退出码必须为 0；绝对可执行文件路径；1–120 秒超时 | 展开后的 argv、退出码、stdout/stderr 各前 32 KiB 及完整输出哈希 |

命令不经 shell；只替换 `{artifact_dir}`、`{input_dir}`。命令和完成条件不能由子模型修改。验证前后对来源和声明产物取哈希，外部 `observed_sources` 也带入阶段输入版本；期间发生变化，哪怕检查通过也保存为 stale 并重新验证。测试非零退出是有证据的失败；超时或异常留下未知预占并停在待核对边界。

`sources` 必须显式包含需要监测的依赖、测试脚本及其他受信任输入。首版不递归监测整个仓库、环境变量、未声明的测试依赖、解释器安装或远端状态；P1 应把额外来源版本交给 `observe_source`。输入与产物目录必须分离，路径每次使用时重新约束在所属根目录内。

## Host 授权和跨执行预算

模型没有创建 Goal、修改定义、扩权、增加预算、取消版本或预算回执 API。显式本地创建即为 Host 授权入口；现有聊天和定时任务不会被自动升级为 Goal 授权。

允许动作固定为 `model/read_file/write_file/verify:file/verify:json/verify:command/replan`；动作与预算分类必须一致。每次实际操作前都检查目标仍 running、阶段身份与 epoch、授权版本、取消版本、到期时间、允许动作及额度。Host 输入定义可修订，但不自动重置任何累计预算或 attempt。

预算分类为 `total/model/tool/validation/replan`。`GoalContext.call()` 先持久预占、再执行、最后结算。`remaining = limit - used - held`；reserved 和 unknown 都计入 held。未知调用不退款；已知返回把原占用移到 used，不重新收费。重复同回执无变化，不同回执不能替换已有结算。跨重启和投影重建保留账目。

模型调用以保守重试包计量：普通 Provider 为 `1 + _max_retries` 个额度单位，返回后也不猜测未用发送并退款；Codex Provider 当前没有该重试字段，按一个逻辑调用单位计。它是调用资格账本，不是精确 token、货币或第三方账单。普通工具、每个验收 criterion 和一次确定性修订准备各记一个单位；子模型的后续收尾调用也经过同一预算包装器。首版重新规划为确定性地携带失败证据进入新 attempt，不新增无约束的规划模型。

取消／撤销立即阻止新预占，不回收在途动作额度。迟到真实回执仍结算、执行结果仍保存；Goal 保持 cancelled。取消不是对已发出的外部操作作撤销保证。未知结果不能通过重启、改 prompt、改标签或修改定义获得自动重做资格；待核对目标首版没有自动“清账／继续”接口。

## P1 可用接口

运行时入口 `core.goal_service`，持久存储为 `core.goal_service.store`。

| 接口 | 契约 |
|---|---|
| `store.create(goal_id, definition, authorization, parent_execution_id="")` | Host 显式新目标；授权不从历史聊天推断 |
| `store.get(goal_id)` / `store.list()` / `store.history(goal_id)` | 状态、账目、执行／证据引用；history 为领域事件 |
| `await service.advance(goal_id, expected_version=version)` | CAS 领取并推进一个阶段；重复旧版本不重复执行 |
| `await service.tick(now=None)` | 恢复死 epoch、检查来源／等待，最多推进一个串行阶段；由现有 scheduler 调用 |
| `service.refresh_inputs(goal_id)` | 对声明来源与产物取版本；无变化无模型调用 |
| `store.observe_source(goal_id, source_id, source_version, expected_version=None)` | P1 经自身来源验证后提交版本；相同版本无状态写入；不调用模型，不直接证明验收通过 |
| `store.wait(goal_id, condition, expected_version=version)` | ready 才可等待；保留当前 step；支持下述两种条件 |
| `store.cancel(goal_id, expected_version=version, revoke=False)` | 增加取消版本；revoke 同时增加授权版本；迟到结果不能重开 |
| `store.revise(goal_id, definition, expected_version=version)` | 保留定义版本；输入根目录不能扩展；预算／attempt 累计不变 |
| `store.recover()` / `store.rebuild()` | 只恢复已有事实或重建投影，不重新执行未知调用 |

等待示例：

```python
store.observe_source(goal_id, "dependency:demo", "v1")
fresh = store.get(goal_id)
store.wait(goal_id, {"kind": "source", "source_id": "dependency:demo"},
           expected_version=fresh["version"])
# 一样的 v1 不唤醒；P1 观察到新的 v2 后才转为 ready。
store.observe_source(goal_id, "dependency:demo", "v2")

# 或等待一个带时区的时间；使用现有调度 tick 唤醒。
fresh = store.get(goal_id)
store.wait(goal_id, {"kind": "time", "until": "2026-11-02T09:00:00+08:00"},
           expected_version=fresh["version"])
```

`__local_files__` 是内部保留来源身份，P1 不应写入。P1 需要自行保存事件订阅、消费游标和来源 provenance；本批只接收经其验证的 source version，不另建通用订阅系统。Goal 不是通知所有者；若 P1 需要通知用户，继续使用已有投递所有权与 Outbox。P0a completion ACK 也不代表 Goal 成功。

## 演示、创建和状态查看

从仓库根目录运行（演示使用新临时目录、固定假执行器、真实本地 Python 验收）：

```powershell
$env:PYTHONPATH = 'apps/backend'
.venv\Scripts\python.exe -m core.goals.cli demo
# 输出独立 workspace、goal_id、完整状态与各阶段结果。
.venv\Scripts\python.exe -m core.goals.cli --workspace 'C:\path\isolated-workspace' show
.venv\Scripts\python.exe -m core.goals.cli --workspace 'C:\path\isolated-workspace' show goal-id
.venv\Scripts\python.exe -m core.goals.cli --workspace 'C:\path\isolated-workspace' cancel goal-id --version 12 --revoke
```

可以用 `demo --workspace` 的全局选项（写法为 `--workspace '新目录' demo`）固定演示目录；CLI 拒绝把演示写入已有目录。未指定时自动生成新临时目录。每个阶段关闭并重新打开 journal，展示重启后的进度和账目。

真实 Goal 入口：

```powershell
.venv\Scripts\python.exe -m core.goals.cli --workspace 'C:\path\authorized-workspace' create goal-spec.json
```

`goal-spec.json` 顶层为 `goal_id/definition/authorization`，可加 `parent_execution_id`。`definition` 示例字段为：`objective/input_dir/sources/artifacts/criteria/max_iterations`；`authorization` 必须显式给 `source/allowed_actions/max_attempts/limits`，可给 `expires_at`。可参照 `core.goals.cli.demo_definition/demo_authorization` 生成有效 JSON。目标创建后由该 workspace 的正常 Host 调度推进；CLI 不会替用户启动 Host，也没有默认使用真实模型的手动 advance 命令。

实际演示证据保存在 [离线演示 JSON](../acceptance/2026-11-01-always-on-p0b-offline.json)：初次执行声称 completed，测试退出码为 1；保存失败证据后修订一次，退出码变为 0，目标才 succeeded。总消耗 9（model 2、tool 2、validation 4、replan 1），held 为 0。JSON 中的临时目录是本次隔离运行位置，不是生产部署路径。

## 验证入口与交接记录

专项：`tests/backend/test_goals.py`，已验证 **17 passed**。覆盖：完成声明与测试失败分离、有预算修订、真实 SubAgent＋假 Provider、文件越界拒绝、Host 动作限制／到期、attempt 上限、来源无变化零模型、来源变化／验收中变化、现有调度唤醒、取消与迟到回执、跨 journal 并发、重复推进／结算、事务回滚、日志／投影重建。另有 unknown_call、completed_step、cancelled 三个真实 `os._exit(73)` 进程故障点；连续两次恢复比对账目和调用计数。

```powershell
.venv\Scripts\python.exe -m pytest tests/backend/test_goals.py -q
```

普通调度与启动接线回归 **81 passed**：

```powershell
.venv\Scripts\python.exe -m pytest tests/backend/test_scheduler_coordination.py tests/backend/bootstrap/test_tools.py tests/backend/agent/test_scheduler_service.py tests/backend/agent/tools/test_schedule.py -q
```

调度回归发现并修复 P0a 的集成缺口：stateless scheduler 推理原本没有父 execution，持久 spawn 因而被拒。现在仅 scheduler 的这条 stateless 路径也使用 `run_recorded`，身份为 `schedule:<job_id>:<fire_at>`；仍不写内部会话历史，既有投递 metadata 保持不变。没有取消 P0a 的父身份要求或重试保护。

扩展 execution／continuation／transport／spawn 恢复回归 **141 passed，323.95 秒**，命令如下；其中包含 Goal 专项，因此不与 17 相加。之后对最后的 Goal 证据／输入边界及声明来源相对路径读取再次跑专项，**17 passed，12.12 秒**。上述 81 项普通调度／接线回归耗时 40.29 秒。

```powershell
.venv\Scripts\python.exe -m pytest tests/backend/test_execution_journal.py tests/backend/test_execution_projection.py tests/backend/test_execution_process_recovery.py tests/backend/test_continuation.py tests/backend/test_continuation_process.py tests/backend/test_spawn_handoff.py tests/backend/test_spawn_completion_flow.py tests/backend/test_durable_transport.py tests/backend/test_execution_stop_notice.py tests/backend/test_goals.py -q
```

Ruff 对新增 Goal 模块、专项测试和修改的 scheduler／processing 路径通过；`git diff --check` 通过。`bootstrap/tools.py` 原有未使用的 `chat_ids_equal` import 会被全文件 Ruff 报 F401，本批未夹带这一无关清理。验证期间 P0a session 另在处理后续复审修复，共享工作树可能继续变化；这里记录的是本批实际完成的测试，不将其他 session 未完成的工作视为已验收，也未替它发布代码。

## 独立 review 后的修复（2026-11-01）

本次 review 确认并修复 1 个 P1、2 个 P2：模型物理发送（含 backoff 后重试）通过上下文 guard 再核对 Goal 的撤销、授权版本、期限与运行阶段；明确的只读文件异常作为已知工具结果正常扣费，允许模型修正路径；验收任务收到取消时保持 phase 所有权，等待已有有界命令结束并保存完整回执后再传播取消。未知写入结果仍保留预占。

新增 `tests/backend/test_goal_review.py`，覆盖真实 Provider 重试循环、错误读取后的成功读取、真实子进程验收期间两次取消与其他 Goal 抢占。与原 Goal 专项合计 20 项通过。交付快照基于已合并 P0a 的 `cf5779c`，不包含并行 P1 输入适配器改动。新增共享边界为 `core/execution/model_recording.py` 的可选上下文 guard；普通模型调用不设置该 guard。

最终提交快照验证：Goal 20 项与调度／启动接线 81 项合计 **101 passed，61.12 秒，退出码 0**；生产及测试 Pyright 均 **0 errors**，目标文件 Ruff 通过。独立 review agent 复审三项修复及集成后未发现新的可行动 P1/P2。快照下默认 pytest 缓存写入收尾停滞，禁用 cacheprovider 后完整退出；未放宽测试断言或运行权限。

## 残余限制与回退

- 只支持串行阶段；当前数据库一次只有一个 active Goal phase，未做多目标公平调度、DAG、策略学习、通用订阅或外部 Codex 执行器。未改 Akasha／Memory2。
- 命令是受信任的 Host 配置；artifact 目录和固定工具不是 OS 安全沙箱。验收脚本及其运行环境由操作者负责，不适合直接执行不受信任脚本。测试超时／进程中断进入核对，不推测副作用不存在。
- 模型／工具结果未知时账目继续占用，恢复不自动重发；没有自动语义续跑。已开始但未提交阶段终态的 Goal 会停为 needs_review，而不是保证任意外部调用恰好一次完成。
- 来源监测仅限显式文件和 P1 输入版本，尚不包含未声明依赖、权限变化、系统环境或远端来源有效性。对应版本需要 P1 或 Host 明确提供。
- CLI 和 `CoreRuntime.goal_service` 是本批入口；未新增桌面 Goal 面板、用户渠道命令或自动通知。真实模型及生产 Host 未验收，不能把离线成功报告成已上线。
- 回退先停止新 Goal 派发，保存完整 execution SQLite、产物及 unknown 预占；不要只拷贝投影。旧代码不认识 `goal.state`，不能直接用旧二进制消费含新事实的数据库。未删除、重置用户或其他 session 的修改。

本批文件：新增 `core/goals/{contracts,store,service,validators,executor,cli,__init__}.py`、`tests/backend/test_goals.py`、本文和离线演示 JSON；最小修改 `core/execution/journal.py` 的领域分流、`agent/scheduler.py` 的内部 tick 与 scheduler 执行身份、`bootstrap/tools.py` 的接线，以及 `agent/looping/core/processing.py` 的 scheduler 记录条件。未接管 P0a manager／queue／handler／schema 的实现。
