# Kaze Device

独立 Windows 10/11 设备上下文 MCP 服务，提供当前 Windows 会话的锁屏状态、输入空闲时间、前台进程和电源信息。它每次查询重新采集，只向 Kaze 的主动链路提供事实快照。

## 准备与接入

需要 Python 3.12+，运行服务的进程应处于用户的交互式 Windows 会话。

```powershell
python scripts/prepare.py --workspace E:/CODE/hasaki-agent/workspace --register --enable-proactive
```

脚本创建独立 `.venv`、安装服务、准备工作区 `device-context` skill 和配置文件，合并 `mcp_servers.json` 与 `proactive_sources.json`。保留其他来源和 MCP；已有不同的 `device` 配置或用户改过的同名 skill 会报告冲突。服务自身管理的未修改 skill 可以升级。已准备环境可加 `--skip-install`。

不使用 `--register` 时，脚本只输出供 Kaze `mcp_add` 使用的 JSON。`--enable-proactive` 合并如下源声明：

```json
{"server":"device","channel":"context","get_tool":"get_context","enabled":true}
```

修改工作区声明后需重启 Kaze 后端。全局主动设置仍决定是否运行及投递目标。

## 工具与快照

- `get_context`：采集当前快照，包含 `observed_at`、`expires_at`、`presence`、`locked`、`idle_seconds`、`foreground`、`power` 和逐项 `errors`。
- `device_status`：查看接口版本、配置、各项探测可用性；不返回应用名称或窗口标题。

`available` 表示至少部分数据可读，`status` 区分 `ok`、`partial`、`unavailable` 和 `stale`。采集本身超过有效期或期间时钟倒退时丢弃观察值，返回 `stale`。`null` 表示未知；硬件无电池时明确返回 `battery_present=false`，电量值为 `null`。前台窗口变化或访问被拒绝时返回该项错误，不沿用旧应用。

快照默认有效 30 秒；超过 `expires_at` 需重新查询。`presence` 的 `active/idle` 由已确认的未锁屏、会话连接状态和空闲阈值派生；锁屏或会话断开标记 `offline`，其含义局限于本机 Windows 会话。条件不足时为 `unknown`。这些标签不代表用户是否同意被打扰。

## 配置

默认配置写入实际工作区的 `mcp-data/kaze-device/config.json`，由 `KAZE_DEVICE_CONFIG` 指定。服务直接运行且没有该环境变量时使用内置默认值。

```json
{
  "idle_threshold_seconds": 300,
  "snapshot_ttl_seconds": 30,
  "foreground_enabled": true,
  "include_window_title": false,
  "game_processes": []
}
```

`game_processes` 可填写用户确认的游戏进程名，例如 `example-game.exe`，精确匹配前台进程时提供 `in_game` 提示。默认列表为空。窗口标题需要显式启用；锁屏或会话状态不明确时跳过前台采集。修改配置后重启 MCP。

## 接口依据

- 锁屏和连接状态：[WTSINFOEX_LEVEL1_W](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/ns-wtsapi32-wtsinfoex_level1_w)，仅查询当前会话。
- 输入空闲时间：[GetLastInputInfo](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getlastinputinfo)，按 DWORD 回绕处理输入时间。
- 前台进程：[QueryFullProcessImageNameW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-queryfullprocessimagenamew)，仅输出可执行文件名。
- 电源与电池：[SYSTEM_POWER_STATUS](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-system_power_status)，将未知标志转换为 `null`。

服务自身不保存观察历史，也没有后台键盘监听或投递功能。Kaze 的 Wake 状态库会保留最新上下文，读取时按有效期过滤。不支持的平台返回不可用快照，其他信息源仍可正常运行。

## 开发验证

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[test]"
.venv/Scripts/python.exe -m pytest -q
```

宿主集成脚本使用 Kaze 的 Python 环境运行，用 `--server-python` 指定本服务的独立 Python。脚本通过真实 stdio MCP 验证当前 Windows 探测、共享工具注册、三路源合并和 Default/Wake 上下文读取，不向渠道发送消息。

```powershell
E:/CODE/hasaki-agent/.venv/Scripts/python.exe scripts/check_kaze.py --backend E:/CODE/hasaki-agent/apps/backend --server-python .venv/Scripts/python.exe
```
