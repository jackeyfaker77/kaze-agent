# Memory2 简化：P0 / P1

本轮收敛既有记忆路径，不合并数据库、不迁移历史记忆、不修改关联笔记或持久任务调度。

## P0：清理没有消费者的接口

- 移除 query_rewriter、sufficiency_checker、profile_extractor、dedup_decider、injection_planner、hyde_enhancer 及对应旧单元测试。最后两个模块一起移除，避免悬空导入。
- 移除 relative_delta、inject_line_max 对应的构造参数、接线和配置输出；旧本地配置中的这些键兼容忽略，不改写用户配置文件。
- P1 取消强制注入后，一并移除 procedure_guard_enabled 和 forced 的配置与构造参数。
- memory2 包不提供包级导出；具体入口仍从其所属模块导入。

## P1：排序与更新只有明确规则

### 检索与注入

普通回忆直接检索用户问题，不再内联调用两次模型生成假设。结果不足时，主循环可以结合对话换表述后再次调用 recall_memory；没有新增“充分性判断”服务。

向量与关键词继续通过 RRF 融合。注入按融合结果的顺序消费，类型阈值和配额只过滤，不重排、不按类型重新分组。procedure 不再凭 tool_requirement 绕过阈值和总字符预算。只注入完整条目；放不下的条目跳过，不截断事实句子。

### 写入与纠错

普通新增保留内容去重与 superseded 防复活；相似度不再导致偏好、画像或流程自动失效，也不再自动合并相似流程。

用户明确纠正已核对的旧记忆时，memorize 可传 `supersedes: [Memory2 ID]`。该关系必须携带当前来源；旧条目必须存在、作用域兼容且仍有效。Notebook ID 不接受此入口，继续使用已有跨存储纠错流程。

新条目、替代关系和旧条目失效在同一事务提交，复用 memory_replacements，不增加表或新引擎。相同来源、相同目标的重复提交不重复记录关系；已失效的新内容不能被该入口复活。失败回滚保留旧条目。持久任务调用该操作时，沿用现有检查点和回执事务。

显式 forget_memory 仍表示失效，不等于撤销替代；遗忘新条目不会自动恢复旧事实。“撤销会话输入”按下述 review 修复维护替代链和来源撤销状态。后台对用户明确废弃旧行为的检测也保留，本轮只移除“新增时凭相似度自动退休”。

`supersedes` 表达模型依据用户纠正选定的关系，程序校验目标与提交条件；这不构成语义正确性的证明。逐字证据与事实/计划等结构字段属于 P2，尚未实现。

## 边界

- Runtime Event Log、source_ref、现有状态、混合召回、持久游标和回执保留。
- 关联笔记及跨存储纠错保持原状。本轮没有达成“所有记忆只有一套事实存储”。
- PENDING / Optimizer、回复后与归档后的提取合并、Markdown 派生视图均未在本轮实施。
- 旧字段数据不批量重写。之前已经被相似度规则退休的条目不会自动恢复。
- 没有新增 reranker，也没有用文件数量或行数变化推断运行性能提升。

## Review 后补齐的兼容边界

- 持久工作流版本升至 2：步骤位置变化后不重放旧版未完成任务。v1 的 pending/running 任务在可安全接管后标为 blocked/unsupported_workflow_version；已有 done 任务和回执不变。发布前应核对并处理积压，不能直接改版本号或重置游标来强制重跑。
- shared 记忆允许同一所有者跨会话纠正；来源 channel/chat 继续保存，角色与记忆域仍须相符，非共享域保留来源隔离。
- 撤销同时匹配条目来源和替代关系来源。复用的既有目标不会因撤销纠正而被删除；同批撤销的中间条目不会恢复。撤销关系保留为 supersede_undone，重复撤销或重放该纠正不会再次恢复/应用它。状态更新与关系撤销原子提交，dry-run 不写入。
- procedure 的 tool_requirement 作为普通工具偏好展示，受相同阈值、顺序和字符预算限制，不构成工具授权。


## 发布后的核验补充（2026-11-02）

### 提交与工作区边界

PR #53 的实现提交为 `9a36f158f5533624e53dfdcf9e4cc770408b056c`，合并提交为 `2648e7278964cacfc8ecd1a8954054481c1ca9e5`。
独立发布分支包含 34 个记忆相关路径，未夹带 desktop、bridge、桌宠等改动。共享 checkout 的其他工作未被 stash、reset 或覆盖；对该目录直接运行 `git diff`，不能代表已合并 PR 的范围。

PR 新增 `test_simplification.py`（358 行）和 `test_review_fixes.py`（312 行）；仅查看 tracked diff 会漏掉提交前仍为 untracked 的测试文件。删减旧测试不能据此解释成“新增代码零测试”。

### 替代写入的四条验收

| 场景 | 可执行测试 | 结果约束 |
| --- | --- | --- |
| 跨作用域替代 | `test_cross_scope_replacement_rejected_before_any_store_change`；`test_private_domain_keeps_source_scope_isolation` | 角色、域或私有来源不匹配时拒绝；shared 同一所有者跨会话纠正仍允许 |
| Notebook ID | `test_invalid_replacement_targets_leave_store_unchanged` 的 `notebook:1` 参数 | 拒绝且存储不变 |
| 被替代的旧条目已失效，且不属于同来源幂等重放 | `test_replacement_rejects_retired_old_target_without_prior_relation` | 新内容尚不存在或已存在且 active 两种情况下均抛出 ValueError；数据库不变 |
| 相同来源与相同替代内容重放 | `test_tool_replacement_records_relation_and_old_content_stays_retired_on_replay` | 返回同一 ID；完整数据库 dump 不变，涵盖条目数、关系数与 reinforcement |

需区分：如果准备写入的新内容本身已退休，现有策略是返回 `skipped:<id>`、不复活它；这不同于“试图替代一个已失效的旧条目”。不能把二者统一描述为必然抛出 ValueError。

### aux_queries 的保留边界

显式 answer/recall_memory 不再生成两条假设。`_query_context` 仍能通过 `MemoryQuery.filters.hints["queries"]` 将调用方给出的附加查询传到 Retriever。当前默认主循环不主动生成额外查询，故保留为按需扩展接口，并非已经启用的自动查询改写能力。

每个去重后的附加查询会增加一次 embedding 与向量召回，不调用 LLM 生成假设；关键词召回只使用原始问题。`test_context_expansion_is_opt_in_and_does_not_generate_queries` 验证默认单查询、显式扩展召回以及重复查询去重。

### 运行观察与表述边界

- 注入阶段新增 `status != active` 过滤，避免失效候选进入上下文；有独立过滤与顺序测试。
- procedure 取消强制注入后可能因阈值或预算被过滤，这是有意取舍，不意味着所有 procedure 都无法注入。应通过固定场景比较“符合条件的记录是否召回、是否进入注入块、工具偏好是否仍可见”；目前没有线上召回率改善或下降的结论。
- “用户要求必须调用某工具”的硬性执行约束不能仅依赖概率召回；本轮没有将已有 procedure 自动迁移为 Skill 或执行门控。
- 简历可以说明显式替代、原子写入、作用域检查、幂等重放及保持 RRF 顺序；不要声称所有记忆已收敛为单一事实存储，也不要把代码体积减少写成性能提升。
- PR #53 CI 后端结果为 2408 passed / 7 skipped，桌面 CI 通过；局部测试数量须同时给出运行范围，不能以 121 表示完整验收。
