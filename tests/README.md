# 当前回归检查

测试以普通会话、传输渠道和工作区共享记忆为边界。`pytest` 使用真实安装的依赖；不再在全局 `conftest.py` 中替换 OpenAI、Telegram 等 SDK。单个测试的网络替身由该测试显式注入。

在仓库根目录安装 `apps/backend/requirements/production.txt` 和 `development.txt` 后运行：

```powershell
.venv/Scripts/python.exe -m pytest -q -W error tests/
.venv/Scripts/pyright.exe --level error
.venv/Scripts/pyright.exe --project pyrightconfig.tests.json --level error
pnpm lint
pnpm typecheck
pnpm test
pnpm --filter kaze-desktop --fail-if-no-match run test:package
pnpm build
```

Linux 中将 `.venv/Scripts/` 换为 `.venv/bin/`。Pyright 测试配置仍保留已有的渐进类型警告策略；以上命令不放宽错误级检查。

关键回归入口：

| 行为 | 测试 |
| --- | --- |
| 会话持久化、迁移幂等、同一会话串行执行 | `backend/test_session_architecture.py` |
| 桌面 RPC、取消后保留部分正文/思考/工具结果与旧历史、重开与重试、重复取消和提交竞态、独立 SQLite 连接验证提交、流式事件先于完成事件和响应 | `backend/test_session_desktop_services.py` |
| EOF 取消、健康检查并发、单一响应写入器 | `backend/desktop_bridge/test_server.py`、`test_request_dispatcher.py` |
| 调度无会话历史推理、成功投递后提交目标历史、失败与取消、规范目标和发送顺序 | `backend/test_scheduler_coordination.py`、`backend/agent/test_scheduler_*.py` |
| 被动回合及回复优先、非被动 FIFO、取消票据和发送窗口释放、回合内工具发送 | `backend/bus/test_chat_lane.py` |
| 全局记忆读写、旧数据兼容、显式渠道过滤、维护任务和失败处理 | `backend/core/memory/test_engine_contract.py` |
| 执行状态重建、原始事实兼容、原子重试接纳、人工核对与重复处置、崩溃后重复重放 | `backend/test_execution_projection.py`、`backend/test_execution_disposition.py` |
| 真实前后台 Shell 超时、换标识重发、成功写入后模型失败、嵌套发送核对与部分成功限制 | `backend/test_execution_review_regressions.py` |
| 真实同步/后台 spawn 安全停止、写入后取消、完成回复丢失重放、撤销许可与接纳重试竞争 | `backend/test_execution_followup_regressions.py` |
| 后台子任务写入后硬退出、已完成回复丢失后的新进程恢复与原请求重放 | `backend/test_execution_process_recovery.py`、`e2e/execution_recovery_crash.py` |
| 认证远程执行处置、重试中断线与重复请求去重 | `backend/bootstrap/test_remote_execution_disposition.py` |
| 队列分段核对、原回执保留、过期/重复操作、账户隔离、旧库升级和处置提交前后硬退出 | `backend/test_transport_review.py`、`e2e/transport_review_crash.py` |
| 双远程客户端队列处置竞争、断线后重复提交 | `backend/bootstrap/test_remote_transport_review.py` |
| 渠道白名单、传输路由和送达状态 | `backend/core/channels/test_hub.py`、`backend/infra/channels/test_clients.py` |
| 心跳配置与忙碌状态、跳过无动作回复 | `backend/bootstrap/test_proactive*.py` |
| 全局桌宠导入、动作校验与并发限流 | `backend/core/pets/test_packages.py`、`backend/plugins/desktop_pet/test_tool.py` |
| 工作流过滤器必须匹配实际包、完整回归必须先于打包 | `backend/test_release_workflows.py` |

GitHub CI 执行完整后端测试、两组 Pyright 和桌面检查。Windows 发布工作流执行相同的后端回归及桌面检查，再构建、验证和计算产物校验和。所有 pnpm 包过滤命令必须带 `--fail-if-no-match`，避免包名变更后静默跳过。

已删除产品模块的历史测试在 [retired/README.md](retired/README.md) 中说明。当前功能的测试继续收集，没有通过新增全局忽略、`xfail` 或关闭警告来绕过失败。

执行工作台的真实 Renderer / preload / RPC 回归在构建后运行：

```powershell
node apps/desktop/node_modules/electron/cli.js tests/e2e/execution_workbench.cjs
```

测试使用隐藏 Electron 窗口、隔离临时工作区和确定性模型，覆盖外部操作禁止直接重试、
键盘输入核对依据、保存处置、安全重试、查看关联执行、事件分页及窄窗口布局。
同时验证待核对错误不恢复已提交输入或附件、显示执行编号，以及更换请求标识仍不能重发。
后续回归还覆盖撤销未使用的重试许可，以及连接退出/超时丢失完成回复后复用原请求标识。
截图和检查结果位于 `.tmp/execution-workbench-*`，不修改生产工作区或配置。

收发队列界面回归：`node apps/desktop/node_modules/electron/cli.js tests/e2e/transport_workbench.cjs`。
隐藏离屏窗口使用真实桥接与隔离 Python 后端，验证未知分段逐项确认、成功回执保留、
重新排队、撤回未执行的重试、关联执行跳转和窄窗口布局。报告与截图在
`.tmp/transport-workbench*`，不会发送真实 Telegram 消息或调用真实模型。
