# Feed 内容源

Kaze Feed 是独立 RSS/Atom MCP 服务，提供订阅管理、后台刷新、正文缓存和版本消费记录。它通过工作区 `mcp_servers.json` 注册普通工具，通过 `proactive_sources.json` 声明主动内容源。

## 接入

在 Kaze 实际工作区安装和注册 `kaze-feed` 后，运行其准备脚本：

```powershell
python mcp/kaze-feed/scripts/prepare.py --workspace . --enable-proactive
```

脚本准备服务环境与工作区 skill，并输出供 `mcp_add` 使用的 JSON。`--enable-proactive` 合并以下源声明，保留其他来源和设置：

```json
{
  "server": "feed",
  "channel": "content",
  "get_tool": "get_proactive_events",
  "ack_tool": "acknowledge_events",
  "get_args": {"limit": 100},
  "enabled": true
}
```

Kaze 启动时读取声明；修改后需重启后端。全局主动设置决定是否运行、目标渠道和检查节奏。后台刷新由 Feed MCP 的生命周期管理，无需额外声明 `poll_tool`。

## 数据与投递

1. Feed 后台采集到期订阅，缓存正文；只有摘要时预取原文。单源失败保留已有缓存，正文失败保留摘要并记录错误。
2. 主动源返回候选快照。`item_id` 是稳定文章 ID，`content_hash` 标识内容版本，候选 `event_id` 是两者组合的确认令牌。
3. Gateway 优先使用候选的 `body` 或完整 `content`，按主动回合的长度限制写入正文缓存；没有正文时尝试网页抓取，失败后使用摘要。
4. 内容版本保留到裁决阶段。具有版本标识的同 URL 更新使用不同投递键；消息语义去重继续执行。
5. 发送成功后，宿主提交历史和本地投递记录，再确认候选。发送失败保留候选。默认主动链路以 `processed` 确认处理，该状态不声明平台回执；显式 `delivered` 确认必须提供真实回执。
6. 旧版本迟到确认只消费旧版本。ACK 失败后，后续裁决可借助已保存的投递记录恢复确认，避免重复发送。

服务升级时自动迁移 SQLite 数据，保留订阅和既有消费状态。普通查询和 `feed_read` 仅读缓存；动态网页或需要登录的正文可通过相应浏览器工具补充读取。

## 验证

服务仓库的单元测试覆盖订阅、增量采集、后台生命周期、正文失败、数据库迁移和版本确认。其 `scripts/check_kaze.py` 使用 Kaze 的真实 MCP 客户端及临时本地 HTTP 服务，验证源声明、Gateway、Resolver、历史提交，以及发送失败和 ACK 失败恢复。

运行集成脚本时，使用 Kaze 的 Python 环境运行宿主代码，使用 `--server-python` 指定 Feed 的独立 Python 环境。测试使用本地投递替身，不向实际用户渠道发消息。
