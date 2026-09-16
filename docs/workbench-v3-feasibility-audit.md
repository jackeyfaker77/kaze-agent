# Workbench 对齐 Akashic v3 可行性审计

审计日期：2026-09-16。对象：`E:/CODE/hasaki-agent`；参考：`E:/CODE/akashic_agent` 本地代码及用户提供的三个界面截图。

本次只审计并提出实施方案，没有修改运行逻辑、配置、数据库或启用主动任务。数据库使用 SQLite `mode=ro` 读取；仅统计数量和结构，不复制聊天内容、模型密钥。

## 用户确认后的收缩范围

用户后续要求“收缩就好”。以下范围替代原先分阶段补齐 v3 能力的建议；后文差异分析保留为审计依据，不作为待实施清单。

- 工作台定位为现有记录的只读查看器，沿用 Akashic 的布局与交互。
- 消息面板展示已有会话、seq 顺序、来源字段、原始内容和附带工具链，支持前缀筛选及历史加载。缺失字段显示未记录；不为可见范围筛选新增内部会话体系。
- 记忆检索仅接入当前引擎已经保存的诊断，按实际数据命名和展示。没有可用数据服务的插件面板不展示。
- 当前没有逐次主动检查记录，暂不做 Wake 面板；不新增检查日志、EventMail、准入流程或送达台账。
- 不迁移 v3 内核，不切换记忆引擎，不补后台内部会话持久化，不搭建动态插件面板宿主。
- 保留桌宠、纯会话标识和全局 workspace/memory。此次确认仅更新方案，尚未实施界面改动。

## 结论

可以把工作台改成“运行记录查看器”，采用消息、记忆检索、主动检查三个面板，布局和交互向参考版本看齐。完整复现参考版本的数据语义，需要后端补充记录能力；直接复制前端或插件不能实现。

参考项目的 `plugins/workbench_ui/plugin.py:3` 声明 `api_version = 3`，但工作台插件版本是 `2.0.0`，面板契约为 `workbench.panels.v2`。关键差异是运行时与数据协议，而不是 Windows 或 React 的限制。

建议继续使用现有 Electron 桌面壳、RPC、全局 session/memory 和桌宠，通过只读适配层实现工作台。完整迁移 Akashic v3 内核应作为另一个范围明确的项目，不夹在界面重构中。

## 当前 workspace 的实际情况

以下是磁盘快照，不代表其他目录或已经启动的其他实例：

- `workspace/sessions.db`：6 个会话、10 条消息（5 条 user、5 条 assistant）；2 条消息包含非空 `tool_chain`。
- 会话 metadata 当前包含标题、最近轮次时间、工具调用次数，没有正式的会话可见范围字段。
- `workspace/config.toml`：memory 开启，engine 为空；`bootstrap/memory.py:37` 将空值解释为 `default`。
- `workspace/observe/recall_inspector.jsonl`：10 条 `context_prepare` 记录。这是检索诊断次数，不代表命中十条记忆。
- 默认路径 `workspace/memory/akasha.db` 不存在，当前配置没有选用 Akasha 引擎。
- proactive 配置关闭，间隔配置为 1800 秒。关闭状态下不能将空列表解释成“检查过但没有到期信件”。

## 能力对照

| 面板 | 已有基础 | 缺口 | 判断 |
| --- | --- | --- | --- |
| 消息 | SQLite 会话、顺序 seq、时间、正文、extra、工具链；已有游标历史读取 | 前缀/可见范围查询、完整内部运行持久化、结构化来源和类型 | 普通会话查看改造较小，完整运行记录为中等改造 |
| 记忆检索 | default 的 JSONL 诊断；旧 Akasha 的查询日志、Dense/Ripple 卡片和来源引用 | 桌面只读 API、跨证据搜索、精确呈现消息标记；v3 Completion/情景簇/Pushes 无对应契约 | 当前引擎的诊断可实现，v3 算法指标不能直接对齐 |
| 主动检查 | HEARTBEAT.md 循环、scheduler、message_push | 每次检查及跳过的记录、内部消息关联、结构化送达结果 | 必须先补采集；完整 Wake/EventMail 属于较大内核迁移 |
| 插件面板 | Python 插件和生命周期扩展点 | v3 的服务注入、Web 面板契约和动态宿主 | 可做能力驱动面板，不能直接装载参考项目 v3 插件 |

## 1. 消息面板

现有 `apps/desktop/renderer/src/sessions/Workbench.tsx` 是全局消息表和侧边详情，使用 `messages.list`。桥接在 `apps/backend/desktop_bridge/session_service.py:143` 调用管理员分页查询，默认按时间倒序，不符合所需的会话原始顺序时间线。

可复用：

- `apps/backend/session/store/messages.py:204` 的 `fetch_messages_page`：按 `before_seq` 获取历史窗口，再返回 seq 升序消息。
- `apps/backend/session/store/sessions.py:90` 的 `list_sessions_for_admin`：SQL 聚合消息数、更新时间；可扩展首条摘要、字面前缀过滤、可见范围及稳定分页。
- `apps/backend/session/store/connection.py:32` 起的会话和消息结构：保留原始标识，不需要引入角色实体。

需要注意：

1. 工具调用和结果主要保存在 assistant 的 `tool_chain`，见 `agent/lifecycle/phases/after_reasoning.py:210`。可以按轮次展开，但不能伪造成具有独立时间、seq 和作者的 v3 原始 Message。部分工具结果是 preview，界面应如实注明截断。
2. 当前部分来源保存在消息 metadata 中；`source`、`sender_role` 与 v3 的作者、来源、body.kind 不是同一套协议。新记录应明确写入字段，旧记录缺失时显示“未记录”。
3. `agent/subagent.py:152` 的子任务消息在局部列表中执行，工具作用域 key 使用进程对象标识；不能据此认为已有完整、稳定的内部会话档案。后台任务 trace 和主会话中的结果也不等于全过程。
4. `bootstrap/proactive.py` 和 `agent/scheduler.py:608` 当前在目标会话执行，且省略用户轮次。需要为后台运行增加稳定 run_id、内部会话及其与目标会话的关联，才能显示完整内部处理过程。
5. 可见范围应使用显式 session metadata/schema 字段，不应只凭 `programmatic:` 等前缀判断内部会话。前缀过滤仍是独立功能。

推荐界面：顶部三个页签；左侧“会话前缀 / 可见范围 / 会话列表”；右侧会话 key、读取最新/更早按钮和 seq 升序消息；每条可展开原始字段、工具调用与结果。保持只读。

## 2. 记忆检索面板

当前默认引擎已有可用诊断数据：`apps/backend/plugins/default_memory/plugin.py:33` 记录 `turn_id`、session_key、时间、检索候选、解析出的 injected_items、原始检索块和 retrieval_trace；同文件还支持 `recall_memory` 工具结果。它足以回答“这轮查过什么、有哪些候选、构造了哪些上下文材料”。但不能把构造上下文等同于模型已接收，更不能等同于最终回复采用。

旧 Akasha 在 `apps/backend/plugins/akasha/store.py:911` 和 `:970` 提供查询列表/详情；`engine.py:990` 写入 Dense、Ripple、激活候选、来源引用和仅 500 字的文本块预览。

参考 v3 的 `E:/CODE/akashic_agent/plugins/akasha/recalls.py:50` 起定义 Hit/Recall，记录消息 ID、dense/completion 通道、presented_message_ids、active_basin_count、pushes，并通过 MessageCatalog 读取原始证据。两边不是只改列名就能对应：

- Ripple 不能直接改名为 Completion。
- source_ref_count 不能直接作为“已呈现消息数”。
- 旧激活数量不能作为 v3 情景簇数量，缺失的 Pushes 不能填 0。
- 旧记录的摘要/预览无法补回当时未保存的完整证据与精确呈现集合。

建议当前显示“记忆检索（默认引擎）”，展示真实的查询、候选、上下文材料和原始诊断。仅在选用 Akasha 且数据服务可用时显示 Akasha 专有字段。历史记录仍保留引擎来源，不因切换引擎而重新解释。

搜索需覆盖查询、会话及证据文本；旧 Akasha 当前 q 只匹配 query_text，因此需要后端扩展。JSONL 历史量增大后需要索引或导入诊断专用 SQLite，避免每次按页查询都扫描整个文件。

## 3. 主动检查面板

`apps/backend/bootstrap/proactive.py:7` 当前只是定时读取 HEARTBEAT.md、调用普通 Agent、遇到 NO_REPLY 不推送的循环。文件不存在、会话忙、内容为空时直接跳过，没有逐次检查的持久化记录。

`agent/tools/message_push.py:115` 起可能把渠道未注册或发送失败作为字符串返回；主动循环没有检查返回值。因此新日志不能仅以“调用了 push”或“没有抛异常”判断已发送。应补充结构化送达结果，并区分调用成功、渠道接受、最终送达未知。

参考 v3 的 `plugins/wake/state.py:47` 持久化 wake_attempts、mail_watermark、outcome。`plugins/wake/message_plugin.py:46` 起依赖 MESSAGE_CATALOG、SESSION_ADMISSION、TIMERS、EVENTMAIL、CONTENT、DRIFT、DELIVERY 等服务；当前 Hasaki 的 heartbeat 不能提供这些语义。

可行的最小真实版本：

- 增加 durable attempt/run 表：run_id、来源 heartbeat/scheduler、计划/触发/结束时间、目标会话和渠道、内部会话、结果和原因、送达记录。
- 在所有跳过路径也写记录：忙碌、无 HEARTBEAT 文件、文件为空、NO_REPLY、生成失败、推送失败等。
- 将模型跳过与执行错误分开；进程中断后将未终结记录显示为中断或结果未知。
- 主动检查关闭时明确显示“未启用”；没有日志时显示“尚无记录”。不自动启用任务。
- 历史未记录的检查无法准确追溯，新增日志从功能上线后开始积累。

建议该页称“主动检查”，注明实际来源。只有正式引入 EventMail、准入流程及送达协议后，才增加 Alert/Content/Drift、信箱水位、Content 不足、Admission 未通过等 v3 字段和结果。

## 原扩展方案（已收缩，不作为当前实施范围）

1. **消息视图与只读接口**：复用现有存储，增加会话前缀/可见范围、摘要、seq 游标、raw/tool_chain 展开。验证翻页不重复遗漏、并发新消息不改变已读历史顺序、空结果与读取失败分开显示。
2. **检索诊断适配器**：接入 default JSONL 与旧 Akasha 日志，按能力显示字段和插件可用状态。验证未启用、零次检索、零命中、证据缺失、记录损坏等状态，不能混成空表。
3. **内部运行及主动检查采集**：先建立稳定标识、生命周期和送达结果，再接列表详情。用本地替身测试 busy/NO_REPLY/生成失败/发送失败/中断；无须调用付费模型或实际发送通知。
4. **按需评估 v3 内核迁移**：如果目标包括完全一致的 Completion 检索和 EventMail Wake，再单独设计消息事实、记忆学习状态、插件服务及数据迁移。仅为了工作台外观，无须先做这项迁移。

建议的桌面接口边界为 `workbench.capabilities`、`workbench.sessions.list`、`workbench.messages.page`、`workbench.retrieval.list/get`、`workbench.checks.list/get`，名称为设计建议，尚未实现。读取接口不得触发模型调用、创建会话、切换记忆引擎或重算历史检索。

本次验证限于源码比较、配置白名单字段和只读数据库/日志统计；没有做运行中的桌面 UI 验证，也没有执行运行时迁移或功能测试。
