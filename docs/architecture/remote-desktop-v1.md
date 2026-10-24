# Desktop ↔ Runtime Host v1

独立 Host 可以接受桌面 WebSocket 连接。桌面选用远程模式后不启动本地 Python，
不创建本地 config.toml，不读取或保存本地模型配置。省略远程环境变量时继续沿用
原本地 bridge 模式。本轮不自动迁移工作区、安装服务或修改已有配置。

## 启动

Host 使用已有工作区及配置，额外读取一个独立 token 文件。token 是单用户管理
凭据，可调用该用户的 Agent 与工具；至少使用 32 个随机 ASCII 字符，限制文件权限。
不要放在 URL、命令行参数或日志中。服务端和桌面各持有同一个 token 文件内容。

```sh
.venv/bin/python apps/backend/main.py host \
  --workspace /var/lib/kaze --config /var/lib/kaze/config.toml \
  --remote-port 8765 --remote-token-file /run/secrets/kaze-runtime-token
```

监听固定绑定 `127.0.0.1`。可通过 SSH 本地端口转发访问，或者在服务器部署 TLS
反向代理，把 `wss://your-host/runtime` 转发到 `http://127.0.0.1:8765`，保留 WebSocket
Upgrade/Connection 头。只接受没有 Origin 的原生客户端；不开放浏览器跨域访问。
不要移除 Origin 检查，也不要把明文端口暴露在公网。代理必须校验证书、支持长连接，
可另加连接速率限制。此仓库未安装或配置代理，公网 TLS 部署须另行验收。

桌面启动时使用以下**进程环境变量**（不写入现有配置文件）：

```powershell
$env:KAZE_RUNTIME_URL = 'wss://your-host/runtime'
$env:KAZE_RUNTIME_TOKEN_FILE = 'C:\private\kaze-runtime-token'
pnpm start
```

通过 SSH 隧道时地址可以是 `ws://127.0.0.1:8765`。非本机地址必须使用 `wss://`，
禁止 URL 内嵌凭据、query 或 fragment。设置不全、认证失败或连接失败均不会回退
到本地后端。清除两个环境变量、退出并重新打开桌面即可回到原本地模式。
如需同时启动本地与远程两个客户端，为测试客户端设置独立 `HASAKI_WORKSPACE`，
从而使用独立 Electron 用户目录；该目录只保存客户端 UI 状态，不保存远程会话副本。

## 数据所有权与生命周期

- Host 持有唯一 AppRuntime、DesktopBridgeService、请求调度器及工作区锁。
  远程连接只收发同一套 RPC/事件，不各自创建服务或数据库。
- 会话、执行事件、调度任务保存在 Host 工作区；Calendar MCP 由 Host 的既有
  `mcp_servers.json` 启动，待办通过原有 Agent 工具操作。所有桌面、Telegram 和
  主动提醒访问同一个服务端 Calendar 实例及其数据库。不会自动复制客户端 MCP。
- `tasks.list` 仍表示调度任务，Calendar 待办仍通过对话工具访问，不把两者混为一谈。
- 桌面退出只关闭连接。已经接收的对话继续在 Host 执行、保存并向其他在线客户端
  发布完成事件。停止 Host 则先关闭连接，再取消请求并走原有部分回复持久化流程。
- 客户端自动退避重连（0.5–10 秒）。连接恢复后重新读取会话列表和当前会话快照，
  不缓存写队列、不重发请求。传输中断或超时时显示“结果未知”，避免把可能已执行的
  输入恢复成待发送内容。用户核对记录后决定后续动作。
- 在线增量事件不是持久订阅日志；离线期间的事件通过当前数据库快照补齐。
  Host 崩溃后的中断执行沿用现有执行日志恢复策略，不承诺自动续跑或 exactly-once。
- 选择会话仅影响当前客户端。会话提交、重命名、删除通过广播使其他客户端刷新。
  同一会话并发发送由现有服务拒绝，跨连接可取消该用户自己正在执行的请求。

## 协议和边界

首帧 `{type: "auth", token: "…"}`，5 秒内完成认证；成功返回
`{type: "auth.ok", protocol: 1}`。之后沿用 `{id, method, payload}` RPC，响应和事件
沿用原协议。认证发生在 Electron 主进程，token 不传给 Renderer。
请求最大 1 MiB、全局最多 128 个待处理请求、每连接最多 256 个排队输出事件。
慢客户端会断开，并通过重连快照恢复，不能阻塞其他客户端或模型执行。

v1 支持文本对话、会话/消息/执行记录、工作台、记忆与运行目录、任务读取。
本地文件上传、远程附件下载、桌宠、语音、屏幕观察、Codex 登录、配置编辑未开放；
远程界面禁用相应主要入口，服务端 RPC 也有允许列表。来自服务器的文件路径不会
获得客户端本地文件读取权限。远程 Agent 工具执行位置是服务器，不是用户桌面。

这是单用户共享 Host，token 持有人具有同一用户权限，不是多租户隔离系统。

## 验证

```powershell
.venv/Scripts/python.exe -m pytest -q -W error tests/backend/bootstrap/test_remote_host.py
node --import tsx --test apps/desktop/src/bridge/remoteBridgeClient.test.ts
# 可选：真实 Calendar MCP（使用已有的独立依赖环境）
$env:KAZE_TEST_CALENDAR_PYTHON = "$PWD/extensions/kaze-calendar/.venv/Scripts/python.exe"
node --import tsx tests/e2e/remote_runtime.mjs
```

端到端探针仅在 `.tmp/remote-probe-*` 创建测试工作区，替换模型响应，运行真实
Runtime Host、WebSocket、AgentLoop、SQLite 与可选真实 Calendar MCP 子进程。
覆盖双客户端、断线继续执行、待办共享、Host 重启、自动重连及不重复执行。
实际 Electron 窗口复测同样使用隔离 Host，不更改用户工作区或发送公网 Telegram 消息。
