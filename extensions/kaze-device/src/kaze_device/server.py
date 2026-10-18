import json
import logging
import sys

from mcp.server import MCPServer

from . import __version__
from .config import Settings
from .snapshot import SnapshotService


def create_server(
    settings: Settings | None = None, *, service: SnapshotService | None = None
) -> MCPServer:
    settings = settings or Settings.from_env()
    collector = service or SnapshotService(settings)
    server = MCPServer(
        "kaze-device",
        version=__version__,
        instructions="Read-only Windows device context. Every query samples current facts. Respect observed_at/expires_at and null/unknown fields. The host decides whether to contact the user.",
    )

    @server.tool(structured_output=False)
    def get_context() -> str:
        """采集本机当前 Windows 会话的锁屏、空闲、前台进程与电源状态。

        每次查询重新采集，快照含 observed_at/expires_at。null 和 presence=unknown
        表示未能确认；available 表示数据可读，不代表用户愿意被打扰。
        默认不采集窗口标题。仅返回上下文，不生成通知或推送。
        """
        return json.dumps(collector.capture(), ensure_ascii=False)

    @server.tool(structured_output=False)
    def device_status() -> str:
        """查看设备接口版本、采集设置和本次各项探测的可用性，不返回窗口标题。"""
        snapshot = collector.capture()
        return json.dumps(
            {
                "version": __version__,
                "status": snapshot["status"],
                "available": snapshot["available"],
                "observed_at": snapshot["observed_at"],
                "expires_at": snapshot["expires_at"],
                "errors": snapshot["errors"],
                "probes": {
                    name: snapshot[key] is not None
                    for name, key in (
                        ("session", "session_id"),
                        ("idle", "idle_seconds"),
                        ("foreground", "foreground"),
                        ("power", "power"),
                    )
                },
                "settings": {
                    "idle_threshold_seconds": settings.idle_threshold_seconds,
                    "snapshot_ttl_seconds": settings.snapshot_ttl_seconds,
                    "foreground_enabled": settings.foreground_enabled,
                    "include_window_title": settings.include_window_title,
                    "game_process_count": len(settings.game_processes),
                },
            },
            ensure_ascii=False,
        )

    return server


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    create_server().run(transport="stdio")
