# Akashic 主动链路来源

本次迁移参照本地 `E:\CODE\learn\akashic-agent` 当前源码（2026-10-08），参考仓库 HEAD 为 `98e96e9422d7e622762d9d5cf5e3fde7dc182133`。范围是完整主动链路，保留 Kaze 的宿主、会话、模型、渠道与记忆服务。

迁入源代码遵循 MIT License，原版权归 kachofugetsu09。完整许可保存在 [akashic-proactive-LICENSE](akashic-proactive-LICENSE)。

| 来源模块 | Kaze 路径 | 生效职责 |
| --- | --- | --- |
| proactive_v2、agent/core/proactive_kernel.py | apps/backend 对应目录 | lifecycle、frame、module graph、调度、状态、共享 MCP |
| plugins/default_proactive、proactive_flow | apps/backend/plugins | Default 全管线、候选工具、裁决与去重 |
| plugins/wake_proactive、wake_proactive_flow | apps/backend/plugins | reservoir、ACK 重试、兴趣 / 内容 / Context 驱动、结构化决策 |
| plugins/drift_flow、wake_drift_flow | apps/backend/plugins | 完整 Drift 活动管线与 Wake 调度衔接 |
| core/clock.py、session/embedding_store.py | apps/backend 对应目录 | 可控回放时间与消息向量缓存 |
| skills/create-drift-skill | apps/backend/skills | Drift 活动创作说明 |
| tests/proactive_v2、tests/wake_proactive | tests/backend/proactive_v2/akashic_default、akashic_wake | 参考行为契约回归 |

Kaze 宿主适配包括：配置默认 Wake 与默认桌面渠道、规则每轮重读、JSON 旧源声明编译及独立刷新、插件声明收集和不可变能力目录、共享 ToolRegistry / MCP 连接、完整被动互动的消息向量补齐、旧成功消息的主动历史识别、用户活动检查、成功回执后历史提交及桌面通知顺序。AnyAction 的 enabled 开关、ACK 明确失败结果和旧时间戳处理做了修正。

Kaze 同时提供 Default 与 Wake 的插件组，由 lifecycle 唯一选择运行组。源刷新独立于候选判断；插件热更新的通用管理系统、Akasha 整体改写、角色关系系统和 Akashic 的其他应用界面不在主动链路迁移范围内。

原简化入口不再用于生产。旧 JSON 成功投递与配额文件不删除，新管线的成功历史与持久化状态各自采用上游机制。运行和配置说明见 [主动推送指南](../_handbook/proactive-guide.md)。
