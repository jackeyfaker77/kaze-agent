# Telegram 延迟与 shell 待核对调查（2026-10-28）

## 已确认的现场证据

- PR33 Telegram 验收执行 `14b02243a66443e5bebb1941d1a1d694`：北京时间 14:26:48.009 接纳，14:26:50.250 完成。唯一主模型尝试从 14:26:49.623 到 14:26:50.120，约 0.50 秒。
- 对应收件 `in:telegram:8659319400:6351167021:117` 完成一次；发件 `reply:in:telegram:8659319400:6351167021:117` 完成时累计尝试 4 次，唯一已确认分段回执是消息 118。不能把“尝试 4 次”说成“发送了 4 条”。
- 旧队列表没有尝试明细，成功转换会覆盖最后错误；桌面桥 stderr 只在进程会话内保存，已重启。因此不能再从持久证据区分当时的连接失败、连接池超时或限流，也无法精确量化投递耗时。本次不猜测具体网络根因，不降低退避或超时来掩盖问题。
- 后续提问“你延迟是不是比以前高了”的执行 `87877b33c62d445b9c46445b1f573569` 先用了两条 bash 风格命令，在 Windows cmd 下失败；之后启动 Python 检查，先手动停止一个后台任务，再遇到 `shell_e342f379fd1a` 的 30 秒硬超时。14:29:04 已为 needs_review，早于第二次重启。
- 最后工具结果明确为 `certainty=unknown` / `outcome_unknown=true`。没有重跑旧命令、补造成功结果或关闭该任务；只读进程核对未发现这两条命令残留。无法根据空输出和超时断言 Python 卡在哪一行。

## 本轮修复

1. 队列新增 `transport_attempts`：领取尝试与开始记录同事务提交；状态更新与完成时间、分类、退避记录同事务提交。成功后保留此前尝试。`transport.get` 的 `attempt_history` 返回明细，本地/远程共用入口。
2. Telegram 将安全重试原因分类为 `connect_error`、`connect_timeout`、`pool_timeout`、`rate_limited`，不记录异常原文、请求 URL 或 token。未知读超时保持 needs_review，不重试。旧数据仅显示已有累计次数，不伪造明细；崩溃时未完成的尝试保留空完成结果。
3. shell 描述按平台声明 Windows cmd.exe 或 POSIX /bin/sh；Windows 不再推荐 bash 分号与路径语法。所有 shell 子进程统一 `stdin=DEVNULL`，避免继承桌面桥的协议输入或等待交互输入。该风险已由真实子进程测试复现边界并验证隔离，但不声称它就是旧任务超时的唯一原因。

## 验证与边界

- 发送尝试审计、既有持久队列、主动恢复：26 passed（随后增加了未完成尝试恢复测试）。
- shell 完整测试文件：36 passed，含父进程输入管道保持打开、子进程仍收到 EOF 的真实进程测试。
- 更新后的尝试审计、人工处置、远程处置、受控续跑：40 passed。均使用 `-W error`。
- 按 CI 命令执行两份 Pyright `--level error`，均通过。
- 生产配置四份 SHA-256 与重启前一致，wake 轮询仍为 300 秒；原未知任务仍为 needs_review。
- PR #34 已合并为 `2ade483`，[最终 CI 全部通过](https://github.com/jackeyfaker77/kaze-agent/actions/runs/40648164271)。2026-10-28 14:58 已通过桌面桥重启生产后端并加载，Telegram 连接健康，transport_attempts 表已初始化。独立本地验收只启动一次后台 shell，task_output 返回 done / exit_code=0 / elapsed_ms=2125，输出 stdin_eof=true 和 SHELL_TASK_DONE。两条原未知执行的完整状态及事件数量未变化，配置哈希未变，wake 仍为 300 秒。没有发送新的 Telegram 验收消息；新网络审计的现场样本仍需后续真实发送积累。没有把隔离分类测试说成真实网络根因已确定。
