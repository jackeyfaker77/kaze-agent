---
name: device-context
description: 查询 Kaze 所在 Windows 会话的锁屏、空闲、前台进程和电源状态，辅助判断主动联系时机。
---

# 设备上下文

使用前通过 mcp_list 检查 device 服务，再调用 mcp_device__get_context。诊断使用 mcp_device__device_status。

- 每次查询都重新采集。只在 observed_at 到 expires_at 之间使用快照，过期时重新查询。
- available 表示至少有部分数据可读，不等于用户有空。status=partial 时同时查看 errors。
- null、presence=unknown 和探测失败表示未确认，不能解释为未锁屏、零空闲或电量为零。
- idle_seconds 是当前 Windows 会话最后一次输入后的时间；不能由它断定用户睡着、离开住所或希望收到提醒。
- locked=true 或 session_connected=false 只描述本机该会话，不能据此断定用户在手机等其他渠道离线。
- 前台进程名只表明当前应用；游戏提示仅来自用户配置的精确进程名，不代表游戏进度或在线账号活动。
- 默认不采集窗口标题。此接口不截图、不保存历史、不控制键鼠，也不发送通知。
- 联系与投递继续走 Kaze 的主动规则、门控和统一投递链路。

独立服务源码自带 scripts/prepare.py，传入实际 --workspace。--enable-proactive 声明 context 源；--register 保存到工作区 MCP 配置，后端重启生效。保留已有 Feed、其他 MCP 和用户自定义设置。
