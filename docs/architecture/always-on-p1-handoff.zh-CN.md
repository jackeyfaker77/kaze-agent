# Always-on P1：目标输入订阅与幂等推进

日期：2026-11-01。本批在共享 checkout 中保留所有既有修改；先只读核对审计、规则和调用链，等待 P0a/P0b 实现、验证及两份交接文档齐备后开始实现。P0a 后续复审快照 142 项本地回归通过后开始 P1；P0b 后续复审期间暂停修改其共同文件，保留其补丁。实现阶段未切换分支、reset、创建 worktree、启动或重启生产 Host、调用真实模型／渠道；提交合并由后续独立复审与 CI 门禁控制。

## 实现与接线

新增 `core/goals/inputs.py`、`input_sources.py`、`input_cli.py` 和专项测试。`GoalService` 构造输入存储与来源适配器；现有 scheduler 的 `goals` 内部 tick 先补采输入、事务消费，再按 P0 的正常入口推进一个串行阶段。

- **Wake**：`bootstrap.proactive` 将本 Runtime 的适配器注入 `ProactiveRuntimeScope`；Wake 入库后进行来源验证并捕获输入。alert/content 复用当前注册路由及 `validate_alert_async`；context 要求当前路由仍注册且未过期。关闭 Wake Runtime 会解绑该实例的验证器，不能留用已关闭的来源能力。
- **定时器**：以已有 `ScheduledJob.id + fire_at` 标识一次名义触发。只有显式订阅且 `owner_session == job.session_key` 的任务转为 Goal 输入，不再走该次普通提醒模型／投递路径；soft 目标定时器在名义时间触发，不提前消费。普通任务不变。启动恢复先捕获目标所属的到期输入，再执行原有 misfire 丢弃／推进规则。
- **子任务结果**：读取 P0a 已提交的 completion payload，或 P0 Goal 已提交的阶段结果，保留 job/child execution、结果和产物引用。来源版本使用该 execution 的事实序号；后续事实若仅改变无关字段，订阅投影会合并。非 completed 执行只记 `result_unresolved` 收据，不取得下一次目标执行资格；completed 同样不代表 Goal 验收通过。
- **快速提示与补偿**：新输入提交后仅在相关字段变化时提示现有 scheduler 立即启动 `goals` 内部 tick；同名在途任务合并。提示是内存优化，丢失后由原有约一秒 tick 补偿。Wake 继续使用原有来源轮询；已提交子结果由内部 tick 补采，不另建完成队列、cron 或消息中间件。

执行仍由 P0 `GoalService.advance → GoalStore.claim → GoalContext.call → finish` 负责。P1 不新设模型授权、重试额度或取消账目。模型只收到本次输入快照及版本，继续使用已有 Kaze SubAgent 的固定文件工具和 P0 独立验收器。

## 稳定身份、版本与无关变化

输入身份是 `(owner_session, kind, source_id, item_id, version)`，同会话同版本重复捕获返回原 seq。来源顺序单独用非负整数 `position`，不把接收顺序或字符串字典序当来源顺序。每个 `(owner_session, kind, source_id, item_id)` 保存最高 position，迟到或同位置不同版本记录为 stale，不能覆盖较新输入。Wake/timer 底层 `publish` 必须显式提供已验证的 `owner_session`；通知归属、基线、去重、消费与快速提示均使用该会话维度。

| kind | source_id / item_id | version / position |
|---|---|---|
| wake.alert/content | 注册的 ACK 路由 / 上游 event_id；兼容 reservoir canonical ID | 原生 revision/version，或规范内容摘要；顺序优先 source_sequence、数字 revision，再用带时区的 updated_at/observed_at/published_at/triggered_at |
| wake.context | 注册的 `_source` 路由 / 同一路由 | 同上；不使用本地入库时间充当来源版本 |
| timer | ScheduledJob.id / 同一 job ID | 名义 fire_at / UTC 微秒时间 |
| result | P0a job_id 或 P0 phase_id / child turn_id | execution 事实序号 / 同一事实序号 |

没有可排序元数据的旧事件视为该 item 的一次不可变版本（position 0）；后续同 item 内容变化没有顺序证据时保守记 stale。需要可变更新的源必须提供整数序号、数字版本或带时区的来源时间。不能凭 opaque version 推断 v2/v10 或不同版本分支的先后关系。

订阅必须显式给 `fields`，支持对象字段的点路径，以及可选精确 JSON `filters`。按 item 分别比较这些字段的投影摘要，Goal 来源版本使用稳定的 item→摘要映射；排序分数、first_seen_at 等运输元数据不会制造新的内容版本。字段未变、filter 不匹配或旧版本只产生相应收据，不调用目标模型。需要按原生版本本身唤醒时，把 `revision`/`version` 加入 fields。

## 权威存储、事务与取消

所有新增表均在原 `private_runtime/execution/events.sqlite3`，没有另一份 Goal 权威库：

- `goal_input_events`：不可更新／删除的捕获输入、来源身份、版本、position、载荷和 stale 标志。
- `goal_input_heads`：每个来源 item 的最高顺序位置。
- `goal_input_subscriptions`：目标、来源、相关字段、filter、所有者会话、active、消费游标、每 item 的基线／顺序和最后语义摘要。
- `goal_input_receipts`：不可更新／删除，唯一键为 `(subscription_id, event_seq)`，保存 applied/unchanged/filtered/stale/baseline/expired/invalid_expiry/result_unresolved/goal_terminal、目标版本和输入摘要。

消费与 P0 的来源观察共享 `BEGIN IMMEDIATE`：读取目标当前版本、写 `goal.state`、输入快照、收据与消费游标一起提交。`GoalStore._observe_source` 和 `_wait` 是公共方法复用的事务内入口，用于加入订阅事务；没有复制另一套状态机。收据写入失败会回滚目标变化；提交后进程退出则由收据／游标跳过重复消费。之后执行派发由 P0 的 CAS 和预算预占约束，不在输入事务中调用模型、工具或渠道。

多 journal 并发消费共享同一 SQLite 事务与唯一收据。重建 execution/Goal 投影不会清空订阅与收据，完整备份必须保留整个 SQLite。取消订阅永久保留原 ID，不能复用；新订阅从当前捕获高水位与本地 Wake 快照建立基线，不自动回放历史数据。取消游标还保留此前已捕获版本的通知所有权，覆盖“捕获后、消费收据前取消”的窗口；未来版本不会取得旧订阅资格。

取消订阅只停止该来源的未来消费，不撤销已经接纳的目标动作。需要停止整个目标时使用 P0 的 `store.cancel(..., revoke=True)`。Goal 已 cancelled/failed/needs_review 时，新输入记录为 goal_terminal，不改变状态；授权过期和预算耗尽仍由 P0 拒绝新调用，账目不会因换来源版本而重置。

## Goal 与 Wake／投递的责任

Goal 专属 alert/content 在**成功持久捕获后**由 Wake 的现有 `consume_and_queue_ack` 消费，ACK 继续走现有注册路由。不会再进入普通 Wake 生成／投递。Goal 专属 context 不产生同一输入的 context reevaluate 通知，但仍可作为现有环境状态。所有权按原会话精确匹配，不能跨会话偷取通知。

P1 不创建用户通知、伪造 delivery_id、物理发送回执或成功状态。普通 Wake、scheduler 和 P0a 父接续的已有 Outbox／渠道所有权保持原职责。子结果订阅只是读取已提交事实，不领取、ACK 或替换 P0a completion intent；P0 自有的 Goal SubAgent 本来就不走普通聊天通知。

Wake 入库后、Goal 捕获前中断时，轮询可从已消费的 reservoir 补读；重新取得来源验证后才捕获，避免上游 ACK/移除导致本地交接丢失。没有当前 Wake 验证器、来源不可用、已撤销或已过期时保持等待，不把这些状态解释成成功。损坏的 Wake 存储只停该来源补采，不阻断无关 timer/result 目标。

## Host 入口与可重复演示

正常 Host 的入口仍为 `core.goal_service`，新增：

```python
service.input_sources.subscribe(
    "dependency-release", "dependency-patch",
    kind="wake.content", source_id="feed_plugin:main", item_id="dependency",
    fields=["release.version", "release.commit"], filters={"package": "demo"},
    owner_session="原来源所属会话", expected_version=store.get("dependency-patch")["version"],
)
service.inputs.get("dependency-release")
service.inputs.receipts("dependency-release")
service.inputs.cancel("dependency-release")
```

订阅创建会把 ready 且未在途的 Goal 原子置为 waiting，等待别名为 `input:<subscription_id>`。后续按 P0 执行／验收，不把历史对话、普通计划或来源事件自动提升为授权。`inputs.publish(...)` 是受信任 Host 适配器的底层捕获接口，不注册为模型工具；调用者必须先完成来源验证。使用 Wake 来源时优先用上面的 `input_sources.subscribe`，让历史快照进入基线。

CLI 订阅 spec 是上述参数组成的 JSON，顶层包括 `subscription_id/goal_id/kind/source_id/fields/owner_session/expected_version`，可加 `item_id/filters`：

```powershell
$env:PYTHONPATH = 'apps/backend'
.venv\Scripts\python.exe -m core.goals.input_cli --workspace 'C:\path\authorized-workspace' subscribe subscription.json
.venv\Scripts\python.exe -m core.goals.input_cli --workspace 'C:\path\authorized-workspace' show dependency-release
.venv\Scripts\python.exe -m core.goals.input_cli --workspace 'C:\path\authorized-workspace' cancel dependency-release

# 完全离线：新临时目录，假执行器、真实 SQLite 与本地 Python 验收。
.venv\Scripts\python.exe -m core.goals.input_cli demo
```

demo 不启动 Host，也不读取生产数据。可用全局 `--workspace '新目录' demo` 固定新目录；拒绝写入已存在目录。演示依次覆盖：v1 基线空闲三轮零模型 → v3、乱序 v2、重复 v3 → 首次产物测试失败 → 关闭／重开 journal → 有预算的修订 → 测试成功 → 重复输入零新增调用 → 取消目标和订阅后迟到输入零调用。

本次实跑证据：[P1 离线 JSON](../acceptance/2026-11-01-always-on-p1-offline.json)。结果 succeeded、两个 attempt；测试退出码 1 → 0；累计额度 total 9（model 2/tool 2/validation 4/replan 1），held 0；idle/duplicate/cancelled model calls 均为 0。临时 workspace 路径只属于该次离线演示。

## 验证记录

专项入口：

```powershell
.venv\Scripts\python.exe -m pytest tests/backend/test_goal_inputs.py -q
```

最终专项为 **26 项**，使用假模型、真实 SQLite、真实固定 Python 测试和三个 `os._exit(74)` 子进程点（捕获后、消费提交后、阶段完成后）。覆盖重复／乱序、无关字段和 filter、事务回滚、并发唯一收据、停机写入／重启补消费、投影重建、取消与新订阅基线、过期输入、P0 预算、三类 Wake 与 ACK、已消费 reservoir 补读、来源验证失败、timer 所有权和过期补捕获、快速提示合并、P0a 结果观察不改 ACK、未知结果不当成功、真实 Kaze SubAgent 读取输入快照。最后补充的场景证明：Goal 专属 context 不占用普通通知节流资格，混合 context 保留非 Goal 变化；取消后已捕获 timer 不退回普通通知路径。

兼容回归 **114 passed**（76.42 秒），包含 P0 Goal、普通 scheduler、调度协调和完整 Wake runtime；另有 **31 passed**（28.21 秒）的 bootstrap/Wake 状态测试。它们可能重叠，不与专项简单相加：

```powershell
.venv\Scripts\python.exe -m pytest tests/backend/test_goals.py tests/backend/agent/test_scheduler_service.py tests/backend/test_scheduler_coordination.py tests/backend/proactive_v2/akashic_wake/test_runtime.py -q
.venv\Scripts\python.exe -m pytest tests/backend/bootstrap/test_proactive.py tests/backend/bootstrap/test_proactive_wiring.py tests/backend/bootstrap/test_tools.py tests/backend/proactive_v2/akashic_wake/test_state.py -q
```

最终合并回归 **171 passed，92.22 秒，退出码 0**，包含上述 26 项专项及 Goal、scheduler、Wake、bootstrap 路径，最终代码冻结后统一运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/backend/test_goal_inputs.py tests/backend/test_goals.py tests/backend/agent/test_scheduler_service.py tests/backend/test_scheduler_coordination.py tests/backend/proactive_v2/akashic_wake/test_runtime.py tests/backend/bootstrap/test_proactive.py tests/backend/bootstrap/test_proactive_wiring.py tests/backend/bootstrap/test_tools.py tests/backend/proactive_v2/akashic_wake/test_state.py -q
```

新增输入模块／测试与修改的 scheduler、Wake runtime、scope 的 Ruff 通过；生产目标模块及上述修改路径、专项测试 Pyright 均为 **0 errors**。本批修改路径的 `git diff --check` 通过。最终演示再次实跑并更新离线 JSON；不以其他 session 的 CI 代替本批验证。

## 独立 review 修复（2026-11-01）

确认并修复 1 个 P1、2 个 P2：会话 A 的捕获不能替会话 B 获得来源验证资格；result 订阅在创建事务内对已完成 spawn/Goal phase 建立基线，不能因首次轮询晚于订阅而回放旧结果；wildcard 的无关变化与有效变化均按 item 比较，不再与另一个 item 的摘要混用。

新增 `tests/backend/test_goal_input_review.py`，覆盖跨会话验证拒绝与重启后的分别接纳、timer 隔离、缺少 owner 的捕获拒绝、未捕获的历史 spawn/Goal phase 基线、wildcard 重启及同值跨 item 的真实变化。新增 6 项与原 26 项专项合计 **32 passed，17.45 秒，退出码 0**。本批从未发布的 P1 离线实验数据库使用旧 schema；本次不猜测其会话归属，不迁移或删除它们，重跑演示使用新的隔离 workspace。

独立 review agent 在基于 `13016ff` 的纯 P1 快照复审后，未发现新的可行动 P1/P2；其独立执行新增 6 项与原 26 项测试，**32 passed，17.33 秒，退出码 0**。该快照生产与测试 Pyright 均为 **0 errors**，目标文件 Ruff 通过。

最终纯快照扩展回归 **180 passed，109.45 秒，退出码 0**，覆盖 P1、P0 Goal/复审、scheduler、Wake 和 bootstrap 路径；使用 `-p no:cacheprovider` 避免本机缓存写入权限干扰。

## 残余范围与回退

### P1 后续复审修复（2026-11-01）

- context 捕获和订阅基线在计算身份前复用 `current_context_snapshot` 检查：不可用、过期、时间无效的快照不写入来源顺序，不解除等待，避免归一化丢失 revision 后使用微秒时间戳污染后续版本。
- `source_inputs[alias]` 使用 `{"schema_version": 2, "items": {item_id: envelope}}`。基线和增量消费保留每个独立 item 的最新投影，批量消费、重启后模型收到与聚合摘要一致的完整快照。旧单 envelope 与旧逐 item 基线在下次消费时兼容转换；合法 item 名 `items`、`item_id` 不参与格式判定。
- 新增 context 恢复/晚订阅六例、真实 GoalSubagentExecutor 假 provider 的批量输入重启验证，以及三种旧基线条目命名回归。兼容回归 184 项通过；最后的旧格式小修后专项 42 项通过，Ruff 与目标 Pyright error 检查通过。独立 review 已复核最终生产代码并确认无待修问题；两组测试有重叠，不相加。
- 本修复不自动改写此前已污染的 ledger heads/positions，也不能凭旧单 envelope 凭空补回曾被覆盖的条目。若旧版本曾实际处理这些输入，应先停止相关目标，依据不可变 ledger 人工核对并建立干净订阅；不能将该补丁当作既有受损数据迁移。此轮没有操作生产 Host 或真实模型/渠道。

- 仅显式创建的本地 Goal／订阅；无桌面订阅面板、自动通知、多机消息服务或 DAG。当前沿用 P0 的全库串行 Goal 阶段及预算。
- Wake 停机补偿依赖其持久快照及上游仍能提供的事件；同 item 停机期间被覆盖的中间版本只补最新可见版本。来源产生后又彻底删除、没有任何本地持久事实的事件无法凭空恢复。
- recurring timer 补偿按磁盘保存的名义 occurrence 合并一次，随后保持原 scheduler 推进规则；不枚举所有错过的周期。
- 目前不做来源 ledger 自动清理、订阅 ID 复用或预算退款；不可变记录需要纳入完整备份。回退前停止新 Goal 派发并保留数据库／收据／产物，不能只复制 Goal 投影。
- 输入快照与版本不代替业务完成条件；验收命令必须真实检查相应依赖／产物。P0 对未知执行保守 needs_review，P1 不保证任意外部动作的恰好一次执行。
- 外部执行器不是本批依赖。继续复用 `GoalSubagentExecutor.run(context, state)` 的现有边界及 GoalContext 额度门控，未来适配器仍需稳定提交／查询／取消／重连和产物协议。本批没有新增 Codex 服务、认证或付费依赖；现有 Codex 模型认证不被当成外部执行器。
- 未做真实模型、生产 Host 或真实 Telegram 验收。本批所有既有普通功能兼容结论限于上述离线回归。

本批文件：新增 `core/goals/{inputs,input_sources,input_cli}.py`、`tests/backend/test_goal_inputs.py`、本文与离线 JSON；修改 `core/goals/{store,service,executor}.py` 的事务内观察／等待、输入补采和模型输入快照；修改 `agent/scheduler.py`、`bootstrap/proactive.py`、`proactive_v2/runtime_scope.py`、`plugins/wake_proactive/runtime.py` 的最小接线。其他 session 的 Goal 修复、P0a、用户原 queue/hub、notebook、路线及 experiments 不属于本批。
