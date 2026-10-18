"""Exercise the real device MCP through Kaze's shared source and context consumers."""

import argparse
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import tempfile


async def check(backend: Path, server_python: Path) -> None:
    sys.path.insert(0, str(backend))
    from agent.mcp.registry import McpServerRegistry
    from agent.tools.base import Tool
    from agent.tools.registry import ToolRegistry
    from plugins.default_proactive.gateway import DataGateway
    from plugins.default_proactive.source import McpGatewaySource
    from plugins.proactive_flow.prompt import ProactivePromptBuilder
    from plugins.wake_proactive.runtime import WakeRuntime
    from plugins.wake_proactive.state import WakeStateStore
    from proactive_v2.config import ProactiveConfig
    from proactive_v2.mcp_sources import SharedMcpGateway, fetch_sources_async
    from proactive_v2.source_catalog import workspace_sources
    from prepare import source_config

    root = Path(__file__).resolve().parents[1]

    class FixtureFeed(Tool):
        name = "mcp_fixture_feed__events"
        description = "Local integration fixture"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return json.dumps(
                [
                    {
                        "event_id": "fixture-1",
                        "kind": "content",
                        "title": "本地内容",
                        "body": "缓存正文",
                    }
                ],
                ensure_ascii=False,
            )

    with tempfile.TemporaryDirectory(prefix="kaze-device-check-") as directory:
        workspace = Path(directory)
        source_path = workspace / "proactive_sources.json"
        source_path.write_text(
            json.dumps(
                {
                    "sources": [
                        {
                            "server": "fixture_feed",
                            "channel": "content",
                            "get_tool": "events",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        source_path.write_text(json.dumps(source_config(workspace)), encoding="utf-8")
        tools = ToolRegistry()
        tools.register(
            FixtureFeed(),
            risk="read-only",
            source_type="mcp",
            source_name="fixture_feed",
        )
        registry = McpServerRegistry(workspace / "mcp_servers.json", tools)
        state = None
        try:
            await registry.add("device", [str(server_python), str(root / "run_mcp.py")])
            assert set(tools.get_tool_names_by_source("mcp", "device")) == {
                "mcp_device__get_context",
                "mcp_device__device_status",
            }
            gateway = SharedMcpGateway(workspace, tools)
            status = await gateway.call("device", "device_status", {})
            assert "foreground" not in status and "power" not in status
            if sys.platform == "win32":
                assert status["available"], status["errors"]
            specs = workspace_sources(workspace)
            device = next(item for item in specs if item.spec.server == "device")
            assert device.spec.channels == ("context",) and not device.spec.ack_tool
            source = McpGatewaySource(gateway, specs, content_limit=5)
            data = await DataGateway(
                alert_fn=source.alert_fn,
                feed_fn=source.feed_fn,
                context_fn=source.context_fn,
            ).run()
            assert len(data.context) == 1
            snapshot = data.context[0]
            assert snapshot["context_type"] == "windows_device"
            assert snapshot["_source"].startswith("workspace:device-")
            assert snapshot["expires_at"] > snapshot["observed_at"]
            assert "window_title" not in (snapshot["foreground"] or {})
            assert list(data.content_store.values()) == ["缓存正文"]
            now = datetime.now(UTC)
            builder = ProactivePromptBuilder(
                cfg=ProactiveConfig(),
                memory=None,
                veda_fn=lambda: "",
                workspace_context_fn=None,
            )
            prompt = builder.render_context_block(data.context, now=now)
            encoded = prompt[prompt.index("[") :].strip()
            assert json.loads(encoded)[0]["context_type"] == "windows_device"

            state = WakeStateStore(workspace / "wake.db")
            runtime = WakeRuntime.__new__(WakeRuntime)
            runtime._state = state
            results = state.ingest_context(data.context, now)
            assert results[0].should_contact is False
            if snapshot["available"]:
                assert len(runtime._active_contexts(now)) == 1
            expiry = datetime.fromisoformat(snapshot["expires_at"])
            assert runtime._active_contexts(expiry) == []
            state.ingest_context([{**snapshot, "available": False}], now)
            assert runtime._active_contexts(now) == []
            normalized = state.list_contexts()[0]
            assert (
                normalized.presence == "unknown" and "foreground" not in normalized.raw
            )
            source.begin_tick()
            refreshed = await fetch_sources_async(gateway, specs)
            state.ingest_context(refreshed["context"], datetime.now(UTC))
            state.close()
            state = WakeStateStore(workspace / "wake.db")
            assert len(state.list_contexts()) == 1
            await registry.shutdown()
            tools = ToolRegistry()
            registry = McpServerRegistry(workspace / "mcp_servers.json", tools)
            await registry.load_and_connect_all()
            assert tools.get_tool("mcp_device__get_context") is not None
            print(
                json.dumps(
                    {
                        "ok": True,
                        "native_status": status["status"],
                        "native_probes": status["probes"],
                        "checks": [
                            "stdio_mcp",
                            "read_only_snapshot",
                            "default_title_policy",
                            "shared_registry",
                            "context_source_without_ack",
                            "feed_coexists",
                            "default_prompt",
                            "wake_state",
                            "expiry_boundary",
                            "unavailable_replaces_previous_state",
                            "refresh",
                            "restart_registration",
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            if state is not None:
                state.close()
            await registry.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--server-python", type=Path, required=True)
    args = parser.parse_args()
    if not (args.backend / "agent/mcp/client.py").is_file():
        parser.error("--backend must point to Kaze's apps/backend")
    if not args.server_python.is_file():
        parser.error(
            "--server-python must point to the prepared device Python environment"
        )
    asyncio.run(check(args.backend.resolve(), args.server_python.resolve()))


if __name__ == "__main__":
    main()
