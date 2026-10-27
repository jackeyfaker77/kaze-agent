# Kaze 可靠基础版候选：发布验收与操作说明

日期：2026-10-28。代码基线 `02c0906`（PR #31）加本轮备份隔离补丁；**本轮为发布候选**，最终发布时须记录交付 PR 的合并提交及对应 CI。依赖以该提交的 Python requirements、`pnpm-lock.yaml` 为准。

## 已交付的使用范围

本地桌面、单用户远程桌面和 Telegram 共用 Runtime Host；交互事实、UI 会话和模型历史遵守事件权威边界。后台记忆任务限定为失效主题抽取及隐式长期记忆。主动提醒支持 Wake 单事件／Context 的受控恢复，ACK 失败与未知发送保持待核对。

配置与常规 300 秒轮询保持不变。本地 Host 未运行时，不承诺 Telegram 或主动提醒在线。

## 安装、启动和升级

- Python 3.12，安装 `apps/backend/requirements/production.txt`；开发验收另装 development requirements。
- 桌面使用 Node 22 和仓库声明的 pnpm 版本，`pnpm install --frozen-lockfile`、`pnpm build` 后 `pnpm start`。
- 开发版显式使用当前 `workspace`；独立 Host 的 CLI 默认目录不同，必须按[Host 文档](architecture/runtime-host-v1.md)显式指定工作区与配置。
- 远程连接沿用[远程桌面文档](architecture/remote-desktop-v1.md)；不启动第二份本地后端消费同一机器人。
- 升级前停止原实例、盘点外部领域存储、创建并验证冷备份，记录 Git 提交和依赖锁文件。保留可执行的原版本，构建完成后再启动原工作区并检查健康、历史、渠道和队列状态。
- 本轮仅增加隔离检查与维护 CLI，不新增业务数据库 schema。一般版本回退仍需先检查 schema 兼容性，不能对所有旧版本承诺原地降级。

## 数据恢复与回退

按[冷备份与隔离恢复契约](architecture/workspace-backup-v1.md)操作。恢复到新目录，不覆盖原工作区，不修改生产配置。

恢复数据不等于重新上线：快照外的发送、ACK 和工具结果可能已发生。恢复副本保持隔离，没有一键解除命令；不得删除标记、使用旧二进制绕过或把旧 pending 队列批量改为可重试。安全接管需要额外核对最新外部回执和事实，当前不自动执行。

副本中的绝对配置路径仍指向原位置，离线核对时不启动 MCP。不要因误启动副本而连接原 Calendar／Feed 或 Telegram。备份包含敏感配置，放在私有目录，不提交 Git。

## 发布证据矩阵

| 项目 | 本轮证据／状态 |
|---|---|
| 既有功能基线 | PR #31 CI 后端 2211、桌面 225、打包 5 项通过；此前生产重启及真实 Telegram 验收通过 |
| 冷备份与隔离恢复 | 当前工作区实测 91 文件、10 数据库；逐库逻辑内容一致，原配置哈希不变 |
| 恢复副本启动隔离 | host、bridge、desktop 均返回 3，未取得 Host 所有权、未创建业务工作者 |
| 快照后已发送的旧队列 | 正式 Outbox 回执＋旧快照恢复回归，外部发送计数保持 1；正式 CLI 隔离也有子进程证明 |
| 备份／恢复进程崩溃 | 真实子进程 `os._exit(73)`，未发布最终目录，重复执行可得到新有效备份 |
| 原实例恢复使用 | 本地与真实 Telegram 各一轮完成；收发各一次，后台记忆任务完成，配置哈希不变 |
| 真实发送后的旧快照 | 确认 Telegram 已送达后再恢复旧备份；副本无本轮输入／回执，四种启动方式各重复两次均被隔离，原收发次数不增加 |
| 恢复后在线接管 | 未提供；不能从离线隔离验收推出自动复产安全 |

本轮本机回归记录（Windows，Python 3.12.13，Node 24.20.0，pnpm 10.33.0）：

- 后端完整套件首轮：2227 passed、2 skipped、1 failed（952.70 秒）。唯一失败为旧入口测试将所有 `Path.exists()` 模拟为真，误造隔离标记；已改为真实临时配置文件，不放宽生产隔离规则。
- 修复夹具及补齐后续边界后，备份、CLI、Host、本地／远程及相关支持模块集成复测：64 passed、1 skipped。包括新增“损坏数据库头”“发布前崩溃”“远程 Host 隔离”“隔离配置搭配新工作区”场景。跳过项为 Windows 环境不允许创建符号链接；未将跳过记作通过。
- 最终源码和测试 Pyright：0 errors、0 warnings。桌面：224 passed、1 skipped；打包测试：5 passed。`git diff --check` 通过。
- 提交前独立 review 发现附件被误当数据库规范化的问题；已将收件上传与持久发件附件按原始字节复制，并补齐真实 `snapshot_file` 的 SQLite、非 SQLite `.db`、`-wal`、`-shm`、`.lock` 附件回归。修复后备份专项 24 passed、1 skipped。
- 未重复运行第二次完整后端套件；完整首轮结果与修复后专项结果分别保留，不能描述为最终树一次全量全绿。提交 PR 时仍需 GitHub CI 全量验证。

证据文件为 `.tmp/baseline-backup-full.log`、`.tmp/backup-final-integration.log`、`.tmp/backup-pyright-final.log`、`.tmp/backup-testtypes-final.log`、`.tmp/backup-desktop-tests.log`、`.tmp/backup-package-tests.log`。`.tmp/baseline-build-identity.json` 记录基线提交、实现文件摘要和本机依赖版本；最终可发布提交仍待合并后固定。

实测快照及报告位于 `.tmp/baseline-20261028-backup`、`.tmp/baseline-20261028-restored`、`.tmp/baseline-restore-drill.json`、`.tmp/baseline-after-send-drill.json`、`.tmp/baseline-live-report.json`，均为本机私有验收产物，不进入 Git。`.tmp` 不是长期备份保管位置；发布后应由用户选定持久、受限的备份介质。

## 必须披露的限制

Markdown 历史归档与近期摘要任务恢复仍未完成。未知外部结果不自动核验。任意 Drift 工具点、缺少收尾协议的插件激活不能通用续跑。Akasha 迁移与目标版验收、24 小时服务器部署、Peer Mesh 均不在本轮范围。

本候选是限定范围的可靠基础版，不是路线 Phase C–E 的 Akasha 目标版。
