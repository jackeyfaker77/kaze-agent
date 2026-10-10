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
| 渠道白名单、传输路由和送达状态 | `backend/core/channels/test_hub.py`、`backend/infra/channels/test_clients.py` |
| 心跳配置与忙碌状态、跳过无动作回复 | `backend/bootstrap/test_proactive*.py` |
| 全局桌宠导入、动作校验与并发限流 | `backend/core/pets/test_packages.py`、`backend/plugins/desktop_pet/test_tool.py` |
| 工作流过滤器必须匹配实际包、完整回归必须先于打包 | `backend/test_release_workflows.py` |

GitHub CI 执行完整后端测试、两组 Pyright 和桌面检查。Windows 发布工作流执行相同的后端回归及桌面检查，再构建、验证和计算产物校验和。所有 pnpm 包过滤命令必须带 `--fail-if-no-match`，避免包名变更后静默跳过。

已删除产品模块的历史测试在 [retired/README.md](retired/README.md) 中说明。当前功能的测试继续收集，没有通过新增全局忽略、`xfail` 或关闭警告来绕过失败。
