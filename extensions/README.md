# 外部能力服务

此目录存放可独立安装的 MCP 服务。每项服务使用自己的 Python 环境，通过工作区 `mcp_servers.json` 注册工具，通过 `proactive_sources.json` 声明输入通道；投递和运行策略由 Kaze 的统一主动链路负责。

| 服务 | 能力 | 主动通道 | 接入说明 |
| --- | --- | --- | --- |
| [kaze-device](kaze-device/README.md) | Windows 锁屏、输入空闲、前台应用与电源状态 | `context` | [设备上下文](../docs/integrations/device.md) |
| [kaze-calendar](kaze-calendar/README.md) | ICS 日历订阅、本地待办、到期提醒 | `alert` | [日历和待办](../docs/integrations/calendar.md) |

独立维护的 Feed 服务通过同一协议接入，见 [Feed 内容源](../docs/integrations/feed.md)。
