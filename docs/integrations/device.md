# Windows 设备上下文

`extensions/kaze-device` 是可独立安装的 MCP 服务。它提供当前 Windows 会话的锁屏状态、键鼠输入后的空闲时间、前台应用进程名以及电源与电池信息，接入 Kaze 的 `context` 通道。

## 安装与声明

使用 Python 3.12+ 运行准备脚本，指定实际工作区：

```powershell
python extensions/kaze-device/scripts/prepare.py --workspace E:/CODE/hasaki-agent/workspace --register --enable-proactive
```

脚本安装独立环境、准备设置和 `device-context` skill，并合并工作区 MCP 注册与主动源声明。保留其他配置；不同的同名 MCP 或用户自定义 skill 会报告冲突。已有环境可加 `--skip-install`。

```json
{
  "server": "device",
  "channel": "context",
  "get_tool": "get_context",
  "enabled": true
}
```

后端重启后读取注册和源声明。全局主动设置仍决定是否运行以及投递目标。设备上下文不需要事件消费确认，也不直接发送消息。

## 数据约定

- `get_context` 每次重新采集，不以旧快照替代失败结果；`device_status` 只返回设置、探测可用性及错误摘要。
- 快照包含带时区的 `observed_at`、`expires_at`，默认有效 30 秒。采集超时或期间时钟倒退时返回 `stale` 并丢弃观察值。
- `available` 表示至少部分数据可读，不能解释为用户有空。`null` 和 `presence=unknown` 表示没有确认。
- 未锁屏、会话连接正常且空闲时长可读时，按配置阈值标记 `active/idle`。锁屏或会话断开产生的 `offline` 仅描述本机该会话；非交互式系统会话不代表用户状态。
- 默认返回前台可执行文件名，不返回完整进程路径和窗口标题。标题可通过设置显式开启。锁屏、断开连接或状态不明确时跳过前台采集；采集期间窗口或锁定状态变化时丢弃应用信息。
- 无电池与电池状态未知分别表示；未知电量不会转换为零。物理电量仅作为上下文，不修改主动链路的互动电量模型。

配置文件默认位于工作区 `mcp-data/kaze-device/config.json`，由 `KAZE_DEVICE_CONFIG` 指定。可配置空闲阈值、快照有效期、是否采集前台应用、是否包含标题，以及用户明确配置的游戏进程名。修改设置后需重启 MCP。

服务自身不保存观察历史。Wake 将最新上下文持久化在自身状态库，读取时过滤过期或不可用记录；Default 在生成上下文提示前清除过期、不可用或时间格式不合法的观察值，保留来源与不可用原因。有效期按当前执行时钟判断，慢速抓取和候选调查后会重新检查；回放使用回放时钟。上下文只辅助现有决策，不绕过主动规则、门控或统一投递。

## 验证

独立单元测试覆盖 Windows 原生数据结构、资源释放、时间回绕、电源未知值、锁定竞态、配置保护和 MCP 工具。

```powershell
extensions/kaze-device/.venv/Scripts/python.exe -m pytest -q -c extensions/kaze-device/pyproject.toml extensions/kaze-device/tests
.venv/Scripts/python.exe extensions/kaze-device/scripts/check_kaze.py --backend apps/backend --server-python extensions/kaze-device/.venv/Scripts/python.exe
```

集成脚本使用真实 stdio MCP 和当前 Windows 探测，验证共享工具目录、Feed 共存、Default 提示、Wake 状态持久化、过期边界、失败快照替换和重连。它使用临时工作区，不操作桌面或向实际渠道发消息。锁屏与竞态分支由原生接口边界的替身测试覆盖，不主动锁定电脑。

Win32 接口依据见服务的 [README](../../extensions/kaze-device/README.md)。
