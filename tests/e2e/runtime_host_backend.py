"""Real headless Host/Telegram SDK/proactive kernel with offline wire and model.

Only boundary adapters are replaced. No desktop bridge is imported or constructed.
All files and synthetic channel settings belong to the passed test workspace.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from telegram.ext import Application
from telegram.request import BaseRequest

from agent.config_models import Config, ChannelsConfig, TelegramChannelConfig
from agent.provider import LLMResponse, ToolCall
from agent.scheduler import ScheduledJob
from agent.tools.base import Tool
from bootstrap import app as app_module, tools as tool_module
from proactive_v2.config_loader import load_proactive_config
import main

workspace = Path(sys.argv[1])
scenario = sys.argv[2]
workspace.mkdir(parents=True, exist_ok=True)
wire_path = workspace / "wire.jsonl"


def record(kind, **values):
    with wire_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": kind, **values}) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class TelegramWire(BaseRequest):
    def __init__(self):
        self.fed = False
        self.next_id = 100
    @property
    def read_timeout(self):
        return 1
    async def initialize(self):
        pass
    async def shutdown(self):
        pass
    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        result = True
        if endpoint == "getMe":
            result = {"id": 999, "is_bot": True, "first_name": "Host", "username": "host_test_bot"}
        elif endpoint == "getUpdates":
            await asyncio.sleep(0.05)
            trigger = workspace / "inbound.json"
            result = []
            if trigger.exists() and not self.fed:
                self.fed = True
                result = [{"update_id": 1, "message": {"message_id": 1, "date": 1700000000,
                    "chat": {"id": 43, "type": "private"},
                    "from": {"id": 43, "is_bot": False, "first_name": "Tester"},
                    "text": "host input"}}]
        elif endpoint in {"sendMessage", "editMessageText"}:
            self.next_id += 1
            message_id = params.get("message_id", self.next_id)
            text = str(params.get("text", ""))
            record("outbound", text=text, chat_id=str(params["chat_id"]), message_id=message_id)
            result = {"message_id": message_id, "date": 1700000000,
                      "chat": {"id": int(params["chat_id"]), "type": "private"}, "text": text}
        elif endpoint not in {"deleteWebhook", "setMyCommands", "sendChatAction", "deleteMessage"}:
            raise AssertionError(f"Unexpected Telegram API operation: {endpoint}")
        return 200, json.dumps({"ok": True, "result": result}).encode()


builder = Application.builder
Application.builder = staticmethod(lambda: builder().request(TelegramWire()).get_updates_request(TelegramWire()))


async def model(**kwargs):
    is_passive = any("host input" in str(m.get("content", "")) for m in kwargs.get("messages", []))
    record("model", passive=is_passive)
    if is_passive:
        if scenario == "crash":
            await kwargs["on_content_delta"]({"content_delta": "host partial"})
            os._exit(73)
        return LLMResponse(content="host reply")
    return LLMResponse(content=None, tool_calls=[ToolCall("decision", "send_event", {"message": "host reminder"})])


class Source(Tool):
    name = "host_source"
    description = "Offline persistent alert source"
    parameters = {"type": "object", "properties": {}}
    def __init__(self, name):
        self.name = name
    async def execute(self, **kwargs):
        if self.name.endswith("__fetch"):
            record("fetch")
            # Re-offer the same ID after restart to verify production deduplication.
            return json.dumps([{"event_id": "host-alert-1", "kind": "alert", "title": "host reminder"}])
        record("ack")
        return json.dumps({"ok": True})


config = Config(provider="openai", model="offline", api_key="offline", memory_optimizer_enabled=False,
                channels=ChannelsConfig(telegram=TelegramChannelConfig("123:offline")))
config.proactive = load_proactive_config({"enabled": True, "lifecycle": "wake", "session_key": "telegram:42",
    "default_channel": "telegram", "default_chat_id": "42", "anyaction_enabled": False,
    "message_dedupe_enabled": False})
tool_module.build_providers = lambda *args: (SimpleNamespace(chat=model), None, None)
tool_module._resolve_plugin_dirs = lambda *args: []
original_build = tool_module.build_core_runtime


def build(*args):
    core = original_build(*args)
    core.plugin_manager = None
    core.presence.record_user_message("telegram:42", datetime.now(timezone.utc) - timedelta(days=1))
    for action in ("fetch", "ack"):
        core.tools.register(Source(f"mcp_host_source__{action}"), source_type="mcp", source_name="host_source")
    # Persist one future reminder before starting workers. Subsequent hosts load it.
    if not (workspace / "scheduled").exists():
        core.scheduler.add_job(ScheduledJob(trigger="at", tier="instant",
            fire_at=datetime.now(timezone.utc) + timedelta(seconds=3), channel="telegram", chat_id="44",
            session_key="telegram:44", message="host schedule", name="host-schedule"))
        (workspace / "scheduled").write_text("yes")
    return core


app_module.build_core_runtime = build
main.Config.load = lambda path: config
for name, value in {
    "channels.json": {"allow_from": {"telegram": ["42", "43", "44"]}},
    "proactive_sources.json": {"sources": [{"server": "host_source", "channel": "alert", "get_tool": "fetch", "ack_tool": "ack"}]},
}.items():
    path = workspace / name
    if not path.exists():
        path.write_text(json.dumps(value), encoding="utf-8")
if not (workspace / "config.toml").exists():
    (workspace / "config.toml").write_text('''[[llm.registrations]]
id = "00000000-0000-4000-a000-000000000001"
provider = "openai"
model = "offline"
api_key = "offline"
[proactive]
enabled = false
''', encoding="utf-8")
assert "desktop_bridge" not in sys.modules
sys.exit(main.main(["host", "--workspace", str(workspace), "--config", str(workspace / "config.toml")]))
