# Runtime Host v1：独立常驻后端

后端生命周期现在由 `bootstrap.runtime_host.RuntimeHost` 管理。桌面本地模式
保留原 JSONL/stdin/stdout 协议；独立 `host` 模式不创建或导入 DesktopBridgeServer，
不读取 stdin，不需要 Electron、浏览器或可见窗口。

两种模式复用 AppRuntime：模型、会话、Telegram 等渠道、消息总线、调度器、
主动生命周期和执行事件库都沿用现有实现。关闭 stdin 不会终止独立 Host；
桌面本地模式的 stdin 关闭仍会释放其内嵌后端。Host 是前台服务进程，操作系统
服务管理器负责开机启动和异常重启。

## 启动与停止

从仓库根目录运行。先退出使用这个工作区的 Kaze 客户端，再选择独立模式：

```powershell
.venv\Scripts\python.exe apps/backend/main.py host --workspace ./workspace --config ./workspace/config.toml

# 在另一个终端检查、请求停止
.venv\Scripts\python.exe apps/backend/main.py host-status --workspace ./workspace
.venv\Scripts\python.exe apps/backend/main.py host-stop --workspace ./workspace
```

Linux 使用 `.venv/bin/python`，安装 `apps/backend/requirements/production.txt`
即可，不需要构建桌面端。`deploy/kaze-host.service.example` 提供 systemd 示例；
部署者需准备实际用户、路径和既有配置，再安装服务。仓库不会自动安装或启用服务。

Host 不复制配置、不写入密钥、不改变白名单或主动提醒频率。建议同时传入两个
绝对路径；省略参数仍沿用 CLI 默认值（`config.toml` 与 `~/.shiori/workspace`），
它们与桌面开发版 `./workspace` 不同。会话、待办、调度任务和执行日志仍保存在原位置。

`host-stop` 提交本地停止请求，`requested: true` 不等于进程已经退出；继续检查
`host-status` 的 `running`。请求绑定本次 owner_id，不会停止后续重启的新进程。
它拒绝停止 bridge 模式；桌面仍从原客户端正常退出。独立 Host 也处理 Ctrl+C、
SIGINT 和 SIGTERM，清理完成后返回 0。

## 所有权、状态和故障

- 业务启动前取得 `private_runtime/host/owner.lock` 的操作系统排他锁。
  同一工作区的 host 和 bridge 不能同时启动，冲突退出码为 2。锁文件不删除，
  避免旧进程仍持有被删除的文件时出现两个所有者。
- `private_runtime/host/status.json` 使用临时文件原子替换，包含模式、owner_id、
  PID、worker/渠道状态、执行 root_id/epoch，不包含配置、token 或原始异常消息。
- `host-status` 用 OS 锁核验存活，旧 PID 文件不算运行。`running` 表示锁仍被持有；
  `healthy` 还要求近期状态和检查正常。15 秒未更新标记过期。控制命令成功读取时
  退出码为 0，监控应读取 JSON 状态。
- Host 每秒检查本地停止请求并更新状态，这是文件 I/O，不调用模型，不改变主动
  提醒原有调度或 token 消耗策略。
- AgentLoop、出站总线、调度器或已启动的主动 worker 意外结束时，Host 清理后
  非零退出，供服务管理器重启。可选渠道/主动模块初始化失败保留其他能力，健康
  状态显示失败；Telegram 轮询停止也显示不健康。
- Telegram 在初始化、注册命令或启动 polling 时遇到临时网络故障，先回收半启动
  的 SDK/HTTP 资源，再由 Host 后台按 5、10、20、40、60 秒退避重试（此后保持
  60 秒；Telegram 限流要求更久时遵从 RetryAfter）。不等待重试完成才启动其他
  worker；恢复成功清除当前启动故障，健康状态自动恢复。退出时取消重试。
- 凭据、权限、参数、重复 polling 冲突及清理失败不自动重试。此机制只恢复启动
  失败；运行中的普通网络重连仍交给 SDK，Conflict 或持久收件队列故障不重启绕过。
  收发处理器只在启动成功后注册，已有持久收件和发件 ID、去重与未知结果规则不变。
- 启动取消、后台异常、正常退出均走清理，期间仍持有工作区锁。硬退出后 OS
  释放锁，新 Host 沿用执行事件库的保守恢复规则。

锁只协调同一工作区的新入口，不能识别此前版本已运行的后端，也不能协调不同
工作区或不同服务器复制的同一个 bot token。迁移时先停旧实例，再转移完整工作区
和配置；不要在线拷贝 SQLite 或漏掉 WAL。文件控制接口供有工作区访问权限的本机
用户使用，不是远程认证接口。

## 本阶段边界

独立 Host 已能运行 Telegram 和已配置的主动提醒。本轮没有部署真实服务器、
切换当前桌面、修改本机配置或发送真实 Telegram 测试消息。服务器没有桌面窗口、
麦克风和本机屏幕，相关工具仍受原有可用性检查约束。

原本地桌面使用内嵌后端，与同工作区的独立 Host 互斥。下一阶段已添加可选的
[远程桌面连接](remote-desktop-v1.md)，复用 Host 会话与 Calendar 待办，并提供认证、
在线事件和重连快照。离线写入、Peer Mesh 和服务器部署仍需后续实施。

后续已加入[持久收发队列](durable-transport-v1.md)，覆盖 Telegram SDK 接收确认与统一发件。
执行日志及收发队列仍不等于 exactly-once 投递。崩溃后已接受的输入
和部分回复可查询，未确认副作用需核对，不自动重跑。桌面取消补存仍由原桌面桥
恢复；无桌面 Host 不会假装恢复出未记录的回执。

## 回归与复测

```powershell
.venv\Scripts\python.exe -m pytest -q -W error tests/backend/bootstrap/test_runtime_host.py tests/backend/bootstrap/test_runtime_host_process.py tests/backend/test_main.py
node --import tsx tests/e2e/desktop_lifecycle.mjs
```

真实子进程使用临时工作区，关闭 stdin 后通过生产 CLI 启动 Host。集成夹具仅
替换模型和网络边界，运行真实 Telegram SDK polling、收发处理、AgentLoop、
主动 Kernel、持久去重和调度器：

1. 无桌面收到 Telegram 输入并回复，发送主动提醒和定时提醒。
2. 竞争工作区的桌面 bridge 在启动渠道前被拒绝。
3. 正常停止后重启，root_id 保持、epoch 改变；重复候选不重复调用模型或推送。
4. `os._exit(73)` 后重启，输入和部分回复恢复为 interrupted，不自动续跑。
5. 原桌面 IPC 的取消、重试、语音控制测试替身和重启读库仍正常。

这些是本机真实进程和业务链路测试，公网 Telegram 与服务器部署仍需单独验收。

启动自动恢复的故障注入覆盖 `getMe`、`setMyCommands`、`deleteWebhook` 三个边界：
Host 在渠道不健康时继续提供其他 worker，模拟网络恢复后同一 owner/epoch 自动恢复
健康；接收、回复后重启并再次提供相同 Telegram update，模型和发送计数仍各一次。
`test_telegram_startup_recovery.py` 另覆盖退避上限、RetryAfter、永久错误、半启动
清理、取消、重复启动和待发送队列。测试只使用临时工作区与替代网络边界的真实 SDK，
不会修改现有配置或发送公网测试消息。
