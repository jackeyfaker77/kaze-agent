# Kaze 可靠基础版：发布验收与操作说明

日期：2026-10-28。当前固定源码基线 **`2ade483efecbed6946c6c0925ac470d2c46982e2`**（[PR #34](https://github.com/jackeyfaker77/kaze-agent/pull/34)，包含 PR #32–34）。**限定范围可靠基础版已验收**，不是 Akasha 目标版。原 PR #32 的 `3f421c5` 作为历史验收保留。依赖文件摘要及本机版本快照见[基线清单](releases/reliable-baseline-2026-10-28.json)；Python requirements 含范围约束，清单是验收环境记录，不保证未来任意重装得到相同依赖。

## 已交付的使用范围

本地桌面、单用户远程桌面和 Telegram 共用 Runtime Host；交互事实、UI 会话和模型历史遵守事件权威边界。后台记忆包括失效主题抽取、隐式长期记忆，以及 Markdown 历史归档／近期摘要与默认 memory2 回调的持久恢复。主动提醒支持 Wake 单事件／Context 的受控恢复，ACK 失败与未知发送保持待核对。

配置与常规 300 秒轮询保持不变。本地 Host 未运行时，不承诺 Telegram 或主动提醒在线。

## 安装、启动和升级

- Python 3.12，安装 `apps/backend/requirements/production.txt`；开发验收另装 development requirements。
- 桌面使用 Node 22 和仓库声明的 pnpm 版本，`pnpm install --frozen-lockfile`、`pnpm build` 后 `pnpm start`。
- 开发版显式使用当前 `workspace`；独立 Host 的 CLI 默认目录不同，必须按[Host 文档](architecture/runtime-host-v1.md)显式指定工作区与配置。
- 远程连接沿用[远程桌面文档](architecture/remote-desktop-v1.md)；不启动第二份本地后端消费同一机器人。
- 升级前停止原实例、盘点外部领域存储、创建并验证冷备份，记录 Git 提交和依赖锁文件。保留可执行的原版本，构建完成后再启动原工作区并检查健康、历史、渠道和队列状态。
- PR #33 新增 Markdown 任务／文件回执，PR #34 新增 transport_attempts；升级会初始化持久表。旧二进制不具备这些工作者和安全边界，不能仅 checkout 旧提交便声称已安全回退。先检查 schema 与业务语义兼容性，必要时使用升级前冷备份做隔离核验，不承诺原地降级或恢复副本自动复产。

## 数据恢复与回退

按[冷备份与隔离恢复契约](architecture/workspace-backup-v1.md)操作。恢复到新目录，不覆盖原工作区，不修改生产配置。

恢复数据不等于重新上线：快照外的发送、ACK 和工具结果可能已发生。恢复副本保持隔离，没有一键解除命令；不得删除标记、使用旧二进制绕过或把旧 pending 队列批量改为可重试。安全接管需要额外核对最新外部回执和事实，当前不自动执行。

副本中的绝对配置路径仍指向原位置，离线核对时不启动 MCP。不要因误启动副本而连接原 Calendar／Feed 或 Telegram。备份包含敏感配置，放在私有目录，不提交 Git。

## PR #32 历史发布证据矩阵

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
- 未重复运行第二次完整后端套件；完整首轮结果与修复后专项结果分别保留，不能描述为最终树一次全量全绿。随后 PR #32 最终 CI 已完成：后端 2236、桌面 225、打包 5 项全部通过。

证据文件为 `.tmp/baseline-backup-full.log`、`.tmp/backup-final-integration.log`、`.tmp/backup-pyright-final.log`、`.tmp/backup-testtypes-final.log`、`.tmp/backup-desktop-tests.log`、`.tmp/backup-package-tests.log`。`.tmp/baseline-build-identity.json` 记录基线提交、实现文件摘要和本机依赖版本；该次历史验收提交为 `3f421c5`。

实测快照及报告位于 `.tmp/baseline-20261028-backup`、`.tmp/baseline-20261028-restored`、`.tmp/baseline-restore-drill.json`、`.tmp/baseline-after-send-drill.json`、`.tmp/baseline-live-report.json`，均为本机私有验收产物，不进入 Git。`.tmp` 不是长期备份保管位置；发布后应由用户选定持久、受限的备份介质。

## 必须披露的限制

Markdown 归档／摘要恢复已在 PR #33 补齐；生产两条新任务只触发近期上下文更新，完整归档／摘要模型链的崩溃恢复依据隔离故障矩阵，不冒称已强制归档真实历史。未知外部结果不自动核验。任意 Drift 工具点、缺少收尾协议的插件激活不能通用续跑。Akasha 迁移与目标版验收、24 小时服务器部署、Peer Mesh 均不在本轮范围。

本版是限定范围的可靠基础版，不是路线 Phase C–E 的 Akasha 目标版。

## PR #32 合并后重启验收（历史记录，2026-10-28）

- 重建桌面并正常退出旧实例，启动 `3f421c5`；运行所有权与 epoch 更新，Host 和 Telegram 健康。
- 原有 31 个会话、303 条消息完整；旧消息逐条无修改。两轮测试后为 32 个会话、307 条消息。四个核心数据库完整性检查通过。
- 本地及真实 Telegram 各一轮成功；收件、回复各一次，队列 done，后台记忆任务完成。
- 旧备份隔离副本在 host、bridge、desktop、remote-host 四个入口各尝试两次，均返回 3；配置目录绕过也被拦截，副本文件未变化。
- 备份／CLI／远程 Host 专项 37 passed、1 skipped；跳过为 Windows 符号链接权限，不计为通过。
- 配置哈希未变，当前 wake 模式实际轮询 300 秒。证据：`.tmp/pr32-restart-audit.json`、`.tmp/pr32-live-report.json`、`.tmp/pr32-isolated-drill.json`。
- PR #32 当时固定的是源码与依赖基线，不表示已发布安装包、创建版本标签或完成服务器部署。Markdown 归档与摘要持久恢复不属于该历史基线，现已由下述 PR #33 补齐。

## 当前基础版增量验收（PR #33–34）

| 交付 | 自动化证据 | 本机生产证据与边界 |
|---|---|---|
| PR #33：Markdown 归档与近期摘要持久恢复；合并 b6e4f8d | [CI 全部通过](https://github.com/jackeyfaker77/kaze-agent/actions/runs/40642224999)；36 项专项含 27 个真实崩溃点，各重复恢复两次；275 项相关集成通过 | 原有 32 会话、316 消息无丢失或修改；本地／Telegram 新对话正常，最终 33 会话、320 消息。两个 Markdown 任务完成；再次重启，任务、写入回执及全部 Markdown 文件哈希不变 |
| PR #34：shell 输入隔离、平台语法说明和发送尝试记录；合并 2ade483 | [CI 全部通过](https://github.com/jackeyfaker77/kaze-agent/actions/runs/40648164271)；shell 36 项；发送／主动恢复 26 项；更新后的审计、处置、远程和续跑 40 项。分组存在重叠，不累计为总数 | 同一源码在合并前已加载生产，独立 shell 只启动一次，task_output 2.125 秒返回 done、exit_code=0、stdin_eof=true。Telegram 连接健康，审计表初始化；本次未额外发送 Telegram 测试消息 |

本机证据：`.tmp/pr33-restart-audit.json`、`.tmp/pr33-live-report.json`、`.tmp/pr33-markdown-snapshot.json`、`.tmp/shell-fix-live-acceptance.json`。这些是私有验收产物，不作为安装包内容或长期备份。自动化证据与生产证据分别使用，不替代真实服务器部署证明。

### 保留的风险与待办

- PR33 Telegram 验收消息发件尝试 4 次，只有一份成功回执；旧错误已被覆盖，具体网络原因不可还原。新尝试审计不补造历史，后续真实使用才能积累网络分类证据。
- 两条 shell 相关执行保留 needs_review；没有重跑、自动关闭或改成成功。标准输入隔离修复不等于任意脚本不会超时；也没有证明此前所有延迟均由同一原因造成。
- 恢复副本依旧不能启动线上业务；未知发送、模型、工具结果与文件冲突继续阻塞，不能自动补发或自动核销。
- 当前使用 default_memory；Akasha Phase C–E、复杂 Drift 工具点恢复、24 小时常驻及 Peer Mesh 均不在本版承诺内。语音未配置不影响文本基础版。
- 需由用户选定私有持久备份介质及保管周期。后续部署远程 Host 时单独验收迁移、认证、SSH 隧道和开机守护；当前不修改配置，轮询保持 300 秒。

### 发布判定与下一步

截至上述证据，**当前基础版按限定范围验收并固定源码基线**。本次是文档与基线清单收尾，不是新增安装包、版本标签或 GitHub Release，也不是承诺无缺陷或端到端 exactly-once。

下一步进入实际使用观察，重点记录投递尝试分类、shell 耗时和待核对原因；有新证据再修复。若继续架构升级，按路线先做 Phase C 的 Akasha 契约与副本迁移评估，不直接切换生产配置。
