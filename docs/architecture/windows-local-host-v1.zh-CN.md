# Windows 本机常驻 Runtime Host v1

本机常驻 v1 验收基线为 `ea0578d`（PR #37–39）：MCP 自动恢复与隐藏启动已合并并加载，正常 Host 重启验收通过；最新版本再次整机重启的最终验收已通过，用户未开桌面时收发与提醒正常且未重复。正式证据与用户操作见[本机常驻 v1 最终验收](../acceptance/windows-resident-v1-final.zh-CN.md)。

本方案使用当前 Windows 用户的登录计划任务，沿用原工作区。桌面连接 `127.0.0.1`，关闭或退出桌面只断开客户端，Telegram、主动提醒与后台任务由 Host 继续运行。无需 SSH，不开放公网端口，不修改模型、MCP、渠道或 300 秒轮询配置。

## 进程与权限

- Windows Task Scheduler 直接启动 `.venv/Scripts/pythonw.exe apps/backend/windows_host.py host ...`；入口在同一 Python 解释器中运行生产 Host，没有额外应用级 supervisor。Windows venv 自身可能有启动器 PID。
- 当前用户 `InteractiveToken`、最低权限、登录后延迟 10 秒启动；不保存 Windows 密码，不承诺未登录时运行。锁屏可继续；注销、睡眠、休眠、关机不能继续工作。本部署不改变电源设置。
- 任务无运行时限、不因电池状态停止；登录触发加每分钟保活触发；任务仍运行时忽略重复触发，异常退出后由下一次触发重启。正常停止留下暂停标记，显式 Start 清除；临时工作区锁冲突不设置暂停，旧版 exit_code=2 冲突标记也不会阻断后续启动。隔离恢复拒绝启动，不写日志。此保活不调用模型，不等于主动提醒轮询。挂机但进程未退出的情况不由任务计划程序判定，需查看 Host 健康状态。
- `IgnoreNew` 加工作区所有权锁阻止重复启动。后台使用同一身份的网络和代理环境，渠道暂时不可用沿用现有退避恢复。
- 认证随机 token 仅写入 `private_runtime/windows-host/runtime-token`，该目录 ACL 仅允许当前用户和 SYSTEM；不放在命令行、URL 或 Renderer。日志 10 MiB 滚动，最多 3 个历史文件。
- 实际业务事实仍由原执行事件、会话投影、收发队列及各领域存储持有；部署层不复制数据、不重放请求、不改写未知结果。

## 安装与日常操作

先构建桌面，检查没有进行中的执行，正常退出原桌面后端并确认所有权释放；按既有冷备份流程备份工作区与外部领域数据，再安装。下面命令从仓库根目录执行：

```powershell
pnpm build
./deploy/windows-runtime.ps1 -Action Plan -Workspace E:/CODE/hasaki-agent/workspace -Port 8765
./deploy/windows-runtime.ps1 -Action Install -Workspace E:/CODE/hasaki-agent/workspace -Port 8765
./deploy/windows-runtime.ps1 -Action Start -Workspace E:/CODE/hasaki-agent/workspace
./deploy/windows-runtime.ps1 -Action Status -Workspace E:/CODE/hasaki-agent/workspace
```

Install 注册为禁用状态，不自动启动。它拒绝已有工作区所有者、占用端口、已有任务／连接文件，不覆盖现有部署。Start 启用登录任务并请求启动；返回 requested 不代表已健康，应检查 Status 的 task 与 host。旧桌面必须真正退出，避免单实例唤回旧内嵌后端。

安装会生成 `.desktop/runtime-connection.json`（版本、回环 URL、token 文件路径，无 token 本体）。此后普通桌面启动自动连接同机 Host，沿用原 Electron 用户目录。显式远程环境变量仍优先；不存在连接文件时仍用原本地模式。连接文件无效、token 丢失、连接失败均不回退到内嵌后端。

同机连接暂时沿用远程模式能力：文本、历史、执行处置、工作台等可用；桌宠、语音、桌面配置编辑和本地文件上传仍受远程模式限制。Host 的工具在这台 Windows 执行。

## 停止、升级与回退

```powershell
./deploy/windows-runtime.ps1 -Action Stop -Workspace E:/CODE/hasaki-agent/workspace
# 维护或更新结束后重新启用
./deploy/windows-runtime.ps1 -Action Start -Workspace E:/CODE/hasaki-agent/workspace
# 回退内嵌模式：先退出桌面，再执行
./deploy/windows-runtime.ps1 -Action Uninstall -Workspace E:/CODE/hasaki-agent/workspace
```

Stop 先禁用任务并持久保存停止意图，再等待启动中／排队中的任务退出；Host 所有者出现后发送正常停止请求，同一 owner 不重复发送，所有者变化时重新核对。超时不强杀或删除数据，保持停止意图。Start 会清除暂停并重新启用任务，即使上次停止超时且进程仍运行，也不会遗漏启用或另起一个实例。Uninstall 确认停止后仅删除本部署任务、部署清单和匹配的桌面连接文件；保留全部业务数据、token 与日志。随后正常启动桌面恢复内嵌模式。移动仓库路径前先卸载，避免旧计划任务指向失效解释器。

## 验证

```powershell
.venv/Scripts/python.exe -m pytest -q -W error tests/backend/bootstrap/test_windows_host.py tests/backend/bootstrap/test_runtime_host_process.py tests/backend/bootstrap/test_remote_host_process.py
pnpm desktop:test
# 显式创建临时任务、强制终止其隔离 Host、等待任务计划程序重启，最后卸载
.venv/Scripts/python.exe tests/e2e/windows_runtime_task.py
```

计划任务演练使用临时工作区和离线模型，验证真实异常重启、单实例、历史保留与正常停止。生产切换应另行核对历史逐条一致、配置哈希不变、桌面连接同机 Host、退出桌面后的 Telegram 收发及一次主动提醒。已发送结果未知仍待核对，不承诺外部网络 exactly-once。未实际注销／登录或重启 Windows 时，不将任务配置检查称为整机重启验收。

## 2026-10-29 本机验收

- 已完成冷备份、安装当前用户计划任务及同机连接；旧工作区 root 保持不变。迁移前的 37 个会话全部存在、335 条原消息逐条不变，4 个数据库完整性检查通过；模型、渠道、MCP 与主动源配置哈希未变。
- 完全退出 Electron 后，独立 Host 继续运行；真实 Telegram 新测试收件、回复均为 done，各处理／发送 1 次，用户确认收到。真实 Calendar 提醒发送 1 次、ACK 1 次；Host 正常重启及后续轮询后仍未重复发送。
- 隔离临时工作区的真实计划任务经强制终止后自动重启，历史保留；重复 Start 没有第二个所有者，正常 Stop 后不自行重启。生产只执行正常重启，没有强杀进行中的真实任务。
- 桌面重新打开后自动连接同一个 Host，真实桌面对话完成；连接准备阶段的旧错误提示在恢复连接后正确消失。
- 发现并修复既有 Markdown 整理任务把发送回执误认为历史编辑的问题：仅排除 `delivery_status`、`external_message_id`，真实内容变化仍阻塞。已保存草稿与模型结果复用，旧回执误判可自动恢复；未知模型结果不会因此获准重跑。该生产任务恢复为 done，原两次摘要模型结果沿用旧记录，后续记忆回调是新步骤。
- 最初两条被记忆整理错误阻塞的测试收件保留 needs_review，未自动重新执行。复测使用新的测试消息。
- 单元与真实子进程覆盖回执变化、正文变化、旧阻塞恢复、未知结果保留、重复恢复及各崩溃窗口。完整证据范围见 [验收记录](../acceptance/2026-10-29-windows-local-host.json)。
- 后续审查修复了锁冲突永久暂停、启动中 Stop 漏发停止请求，以及运行中任务未重新启用的问题。11 项生命周期测试和 13 项 Host 进程回归通过（另 1 项 POSIX 测试跳过）；真实计划任务再次验证冲突后强退自动拉起、历史保留、立即 Start→Stop 与单实例。修复后复审未发现新的阻断问题；本轮演练没有中断生产 Host。

上述 2026-10-29 验收未覆盖真实注销再登录、重启 Windows、未登录运行与睡眠唤醒网络恢复。

## 2026-10-30 Windows 重启后补验收

- 用户已实际重启 Windows 并登录，未打开 Kaze 桌面；计划任务启动独立 Host，Telegram 收件和回复均完成一次，用户确认收到。
- 重启暴露 Calendar、Feed 启动失败后没有后台恢复的问题。已补充[连接恢复与未知结果边界](mcp-connection-recovery.md)，并正常重启 Host 加载修复；三个现有 MCP 和 Telegram 均健康。
- 旧主动执行经只读核对及用户批准关闭，保留原未知结果，不重跑。原测试待办随后由新主动执行送达：发送尝试 1 次、ACK 1 条，用户贴回收到的提醒。下一次 300 秒轮询已无该 alert，发送及 ACK 记录未增加。
- 重启前 351 条历史消息全部存在且逐条哈希不变；四份业务配置与重启前基线一致，轮询仍为 300 秒。桌面在本轮后台验收中保持关闭。本地／远程兼容性另由自动化回归验证。
- 故障注入使用隔离的真实 stdio 子进程：初始化超时后自动连接，工具派发后子进程退出，重新连接不重发工具。没有强杀生产业务或制造真实发送未知结果。

仍未覆盖：单独注销再登录、未登录运行、睡眠唤醒网络恢复，以及再次整机重启时故障自动恢复的真实注入；本轮是实际整机重启后发现故障、修复并正常重启 Host 的组合验收，不将隔离故障测试冒充第二次整机故障演练。
