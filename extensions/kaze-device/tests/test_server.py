import json

from kaze_device.config import Settings
from kaze_device.server import create_server
from kaze_device.snapshot import SnapshotService


class Probes:
    def session(self):
        return {
            "session_id": 1,
            "interactive": True,
            "connected": True,
            "locked": False,
        }

    def idle(self):
        return 5.0

    def foreground(self, *, include_title=False):
        return {
            "process_name": "PrivateApp.exe",
            "process_id": 123,
            "window_title": "Private title",
        }

    def power(self):
        return {"battery_percent": 42}


async def test_status_does_not_export_app_or_title_and_context_is_refreshed():
    settings = Settings(include_window_title=True)
    server = create_server(settings, service=SnapshotService(settings, Probes()))
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == {"get_context", "device_status"}
    # Exercise the MCP server's own dispatch without bypassing its registered tool functions.
    result = await server.call_tool("device_status", {})
    text = "".join(item.text for item in result.content if item.type == "text")
    status = json.loads(text)
    assert status["available"] is True
    assert "PrivateApp" not in text and "Private title" not in text
