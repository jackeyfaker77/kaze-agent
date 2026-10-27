# 可靠基础版：一致性冷备份与隔离恢复 v1

## 交付边界

本工具提供静止工作区备份、完整性校验和隔离恢复，不提供在线热备或旧快照自动复产。
Runtime Event Log、队列、记忆和待办各自保留原有权威；备份不是新的业务事实，也不创造模型、工具或发送的执行资格。

旧备份无法证明备份之后哪些消息已送达、哪些工具已执行。即使快照中的 Outbox 是 pending，也不能据此补发。
因此备份包及所有恢复数据根都携带 `.kaze-restore-quarantine.json`：正式 host、bridge、desktop 入口、RuntimeHost、AppRuntime 和 Core 构建均在业务启动前拒绝运行隔离副本。重复启动、重复恢复和损坏的标记都不会解除隔离。
启动入口同时检查配置所在目录；不能通过省略工作区参数、给旧配置搭配一个新工作区来绕过隔离。

本版本没有解除隔离命令，不删除隔离标记，也不把“没有产生新副作用”表述为“旧业务已完成”。
确认外部状态、补齐备份之后的事实及安全重新上线属于后续独立恢复协议。未知结果仍待核对。

## 一致性前提与覆盖范围

1. 先正常停止桌面本地后端／独立 Host，以及所有独立领域写入者；远程客户端断开不等于 Host 停止。
2. 备份全过程持有同一工作区的维护所有权锁；运行中的 Host 会导致备份失败。
3. `--quiesced` 是对独立 MCP／外部数据库写入者已停止的操作确认，不会自动终止未知进程。Host 锁不能约束其他程序。
4. SQLite 使用 Backup API 收纳已提交 WAL，并将副本规范化为独立数据库；不逐个拷贝运行中的数据库后声称获得跨库原子快照。
5. 比较备份前后源文件集合、大小及修改时间；变化则拒绝发布。这是附加检查，不能替代停写前提。

默认递归备份工作区中的事实、会话、队列、记忆、画像、调度、观察、迁移记录、技能及内置 MCP 领域数据与配置。
工作区外的领域数据必须通过 `--data-root NAME=PATH` 显式纳入；主配置若在工作区外，也应纳入对应目录。
工具不会从任意 MCP 参数、插件代码或环境变量自动推断全部外部存储，不能把未声明的数据称作已覆盖。

`uploads` 与 `private_runtime/transport/attachments` 属于不透明附件：按原始字节复制，保留内容摘要及稳定身份。即使附件本身是 SQLite 或使用 `.db`、`-wal`、`-shm`、`.lock` 后缀，也不按运行数据库规范化、过滤或猜测旁文件关系。

每份 manifest 记录实际文件、数据根、排除项、数据库类型、字节数、SHA-256、备份 ID、时间和代码版本说明。
明确排除：工作区 `.desktop`（Electron 缓存／UI 状态）、`.tmp`、`pets`（资源）；各层 `.git`、`.venv`、`node_modules`、`__pycache__`、`.pytest_cache`；运行时锁、Host 状态与停止请求。可重建资源不属于业务数据备份。
其余链接／junction 和特殊文件会使备份失败，不沿链接访问未声明目录。

备份含配置和可能的凭据，应存放在受限的本机目录或加密存储中，不能上传 GitHub。本版本不提供加密或异地备份。校验和用于检测损坏，不是来源签名。

## 命令

从仓库根目录执行；以下使用 Windows，Linux 替换为 `.venv/bin/python`。目标父目录必须存在，目标目录必须不存在。

```powershell
# 确认停机与数据范围后创建；版本来自本次实际核对结果
.venv/Scripts/python.exe apps/backend/main.py workspace-backup `
  --workspace E:/CODE/hasaki-agent/workspace `
  --destination E:/KazeBackups/backup-001 `
  --quiesced --revision <tested-commit>

# 独立领域数据可重复添加：--data-root calendar=E:/CalendarData
.venv/Scripts/python.exe apps/backend/main.py backup-verify --backup E:/KazeBackups/backup-001

.venv/Scripts/python.exe apps/backend/main.py workspace-restore `
  --backup E:/KazeBackups/backup-001 --destination E:/KazeRestore/drill-001
```

恢复结果为 `drill-001/workspace` 及 `drill-001/<外部数据根名>`。**不会写回原位置，不会重写配置中的绝对路径**。
`restore-report.json` 标明隔离状态、来源 manifest 摘要和版本。用离线 SQLite／文件工具核对，不运行原 MCP 配置。
`backup-verify` 不加载运行配置、不创建 Provider 或渠道；未知版本、路径越界、重复清单、额外文件、内容损坏或缺少标记均拒绝恢复。

恢复副本启动时返回退出码 3；业务进程和后台任务未启动。正常工作区不含隔离标记，本地、远程和 300 秒轮询保持原行为。

## 发布与崩溃边界

先写带隔离标记的 `.partial-<id>` 目录，复制并刷盘，校验后再写完成 manifest／报告，最后重命名发布。
目标不得覆盖已有目录，也不能与源重叠。中途错误或强制退出留下的 partial 保留供核对，不能当作正式备份；重新执行使用新目标。
读取备份和复制恢复期间均验证文件清单，恢复副本在第一份数据落盘之前已有隔离标记。

证明范围是实际进程崩溃与重启，不宣称跨平台断电、磁盘固件或恶意管理员删标记后仍安全。禁止用不认识隔离标记的旧二进制启动恢复目录。

| 故障／场景 | 必须结果 |
|---|---|
| Host 正在运行／未确认外部停写 | 拒绝备份 |
| 已提交数据尚在 WAL | 完整进入规范化副本 |
| 文件变化、损坏、额外文件或路径越界 | 不发布可用结果 |
| 备份／恢复中途 `os._exit(73)` | 正式目标不存在，partial 保持隔离 |
| 快照后原队列已发送，快照里仍 pending | 恢复副本不能启动任何工作者，不补发 |
| 重复恢复、多个入口启动 | 每个副本继续隔离，外部调用计数不增加 |
| 原工作区重新启动 | 正常使用；配置与历史不改写 |

测试入口：`tests/backend/bootstrap/test_workspace_backup.py`。其中真实 Outbox 先在备份之后写入发送回执，再恢复旧 pending 快照，通过正式 RuntimeHost／CLI 证明不会再次派发。外部网络使用计数替身，生产数据演练与真实 Telegram 收发另行记录。

## 当前限制

- 没有把恢复旧快照等同于安全接管线上实例；自动复产仍未交付。
- 独立外部服务必须单独停写，不能靠 Host 锁声称获得其一致性。
- Markdown 归档／摘要的任务恢复仍未完成；备份文件不补齐这条执行链。
- 不切换 Akasha、不部署服务器、不扩展 Drift 任意工具点续跑。
