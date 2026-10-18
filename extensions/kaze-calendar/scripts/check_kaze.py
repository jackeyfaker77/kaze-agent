"""Real stdio MCP + loopback ICS integration; all data and delivery are test fixtures."""

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace


async def check(backend: Path, server_python: Path):
    sys.path.insert(0, str(backend))
    from agent.looping.ports import SessionServices
    from agent.mcp.registry import McpServerRegistry
    from agent.tools.base import Tool
    from agent.tools.registry import ToolRegistry
    from agent.turns.orchestrator import TurnOrchestrator, TurnOrchestratorDeps
    from plugins.default_proactive.context import AgentTickContext
    from plugins.default_proactive.gateway import DataGateway
    from plugins.default_proactive.resolve import ProactiveResolver, build_delivery_key
    from plugins.default_proactive.source import McpGatewaySource
    from plugins.wake_proactive.state import WakeStateStore
    from proactive_v2.config import ProactiveConfig
    from proactive_v2.mcp_sources import (
        SharedMcpGateway,
        source_key,
        validate_alert_async,
    )
    from proactive_v2.source_catalog import workspace_sources
    from proactive_v2.state import ProactiveStateStore
    from session.manager import SessionManager

    root = Path(__file__).resolve().parents[1]
    now = datetime.now(UTC)
    begin = (now + timedelta(minutes=10)).strftime("%Y%m%dT%H%M%SZ")
    end = (now + timedelta(minutes=40)).strftime("%Y%m%dT%H%M%SZ")
    fixture = {"title": "本地测试会议", "cancelled": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = (
                "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Kaze Check//EN\r\n"
                "BEGIN:VEVENT\r\nUID:meeting-1\r\n"
                f"DTSTART:{begin}\r\nDTEND:{end}\r\nSUMMARY:{fixture['title']}\r\n"
                + ("STATUS:CANCELLED\r\n" if fixture["cancelled"] else "")
                + "END:VEVENT\r\nEND:VCALENDAR\r\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class FixtureFeed(Tool):
        name = "mcp_fixture_feed__events"
        description = "Offline fixture"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return json.dumps(
                [{"kind": "content", "event_id": "article-1", "body": "缓存正文"}]
            )

    class FixtureContext(Tool):
        name = "mcp_fixture_context__get_context"
        description = "Offline fixture"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return json.dumps({"available": True, "presence": "active"})

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="kaze-calendar-check-") as directory:
            workspace = Path(directory)
            (workspace / "proactive_sources.json").write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "server": "fixture_feed",
                                "channel": "content",
                                "get_tool": "events",
                            },
                            {
                                "server": "fixture_context",
                                "channel": "context",
                                "get_tool": "get_context",
                            },
                        ],
                        "preserved": True,
                    }
                ),
                encoding="utf-8",
            )
            # Exercise the actual installer and its config environment variable.
            prepared = subprocess.run(
                [
                    str(server_python),
                    str(root / "scripts/prepare.py"),
                    "--workspace",
                    str(workspace),
                    "--skip-install",
                    "--register",
                    "--enable-proactive",
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            specification = json.loads(prepared.stdout)
            assert "KAZE_CALENDAR_CONFIG" in specification["env"]
            assert json.loads(
                (workspace / "proactive_sources.json").read_text(encoding="utf-8")
            )["preserved"]
            assert (workspace / "skills/calendar-todo/SKILL.md").is_file()
            tools = ToolRegistry()
            tools.register(
                FixtureFeed(),
                risk="read-only",
                source_type="mcp",
                source_name="fixture_feed",
            )
            tools.register(
                FixtureContext(),
                risk="read-only",
                source_type="mcp",
                source_name="fixture_context",
            )
            registry = McpServerRegistry(workspace / "mcp_servers.json", tools)
            state = ProactiveStateStore(workspace / "proactive.db")
            wake = WakeStateStore(workspace / "wake.db")
            sessions = SessionManager(workspace)
            delivery = SimpleNamespace(ok=False, calls=0)

            class Outbound:
                async def dispatch(self, message):
                    delivery.calls += 1
                    return delivery.ok

            orchestrator = TurnOrchestrator(
                TurnOrchestratorDeps(
                    session=SessionServices(session_manager=sessions),
                    outbound=Outbound(),
                )
            )
            try:
                await registry.load_and_connect_all()
                assert len(tools.get_tool_names_by_source("mcp", "calendar")) == 10
                gateway = SharedMcpGateway(workspace, tools)
                gateway.consumer = "desktop:calendar-check"

                async def call(tool, **args):
                    return await gateway.call("calendar", tool, args)

                await call(
                    "calendar_manage",
                    action="subscribe",
                    name="本地夹具",
                    url=f"http://127.0.0.1:{http.server_port}/calendar.ics",
                )
                assert (await call("poll_calendars", force=True))["ok"]
                task = await call(
                    "todo_create", title="本地测试待办", due_at=now.isoformat()
                )
                specs = workspace_sources(workspace)
                calendar = next(
                    item for item in specs if item.spec.server == "calendar"
                )
                source = McpGatewaySource(gateway, specs, content_limit=5)
                data = await DataGateway(
                    alert_fn=source.alert_fn,
                    feed_fn=source.feed_fn,
                    context_fn=source.context_fn,
                ).run()
                assert len(data.alerts) == 2 and len(data.context) == 1
                assert list(data.content_store.values()) == ["缓存正文"]
                alert = next(item for item in data.alerts if item["type"] == "calendar")
                old_token = alert["event_id"]
                assert (
                    await validate_alert_async(
                        gateway, specs, alert, now=datetime.now(UTC)
                    )
                    == "valid"
                )

                wake.ingest("alert", data.alerts, datetime.now(UTC))
                stored = wake.unread("alert")
                assert len(stored) == 2
                assert (
                    await validate_alert_async(
                        gateway, specs, stored[0], now=datetime.now(UTC)
                    )
                    == "valid"
                )

                # A persisted alert is rechecked even after a later cancellation.
                fixture["cancelled"] = True
                assert (await call("poll_calendars", force=True))["ok"]
                assert (
                    await validate_alert_async(
                        gateway, specs, alert, now=datetime.now(UTC)
                    )
                    == "invalid"
                )
                fixture["cancelled"] = False
                fixture["title"] = "已改期说明的会议"
                assert (await call("poll_calendars", force=True))["ok"]
                await call("acknowledge_events", event_ids=[old_token])
                current = await call("get_proactive_events")
                updated = next(item for item in current if item["type"] == "calendar")
                assert updated["event_id"] != old_token

                todo_token = next(item for item in current if item["type"] == "todo")[
                    "event_id"
                ]
                await call(
                    "todo_update", todo_id=task["id"], changes={"status": "completed"}
                )
                assert (
                    await call("validate_proactive_events", event_ids=[todo_token])
                )["valid_event_ids"] == []

                resolver = ProactiveResolver(
                    cfg=ProactiveConfig(message_dedupe_enabled=False),
                    session_key=gateway.consumer,
                    state_store=state,
                    deduper=None,
                    recent_proactive_fn=None,
                    ack_fn=source.ack_fn,
                    alert_ack_fn=source.alert_ack_fn,
                    validate_alert_fn=source.validate_alert_fn,
                )

                async def context():
                    source.begin_tick()
                    events = await source.alert_fn()
                    assert len(events) == 1 and events[0]["type"] == "calendar"
                    ctx = AgentTickContext(
                        terminal_action="reply", final_message="十分钟后有测试会议。"
                    )
                    ctx.fetched_alerts = events
                    ctx.cited_item_ids = [
                        f"{source_key(calendar)}:{events[0]['event_id']}"
                    ]
                    return ctx

                async def dispatch(result):
                    return await orchestrator.handle_proactive_turn(
                        result=result,
                        session_key=gateway.consumer,
                        channel="desktop",
                        chat_id="calendar-check",
                    )

                ctx = await context()
                decision = await resolver.resolve(ctx)
                assert not await dispatch(decision.result)
                assert not sessions.get_or_create(gateway.consumer).messages
                assert len(await call("get_proactive_events")) == 1

                real_call = gateway.call

                async def lost_ack(server, tool, args, **kwargs):
                    if tool == "acknowledge_events":
                        raise ConnectionError("Injected ACK failure")
                    return await real_call(server, tool, args, **kwargs)

                gateway.call = lost_ack
                delivery.ok = True
                assert await dispatch(decision.result)
                assert len(sessions.get_or_create(gateway.consumer).messages) == 1
                assert len(await call("get_proactive_events")) == 1
                gateway.call = real_call
                before = delivery.calls
                retry = await resolver.resolve(await context())
                assert retry.action == "skip" and not await dispatch(retry.result)
                assert delivery.calls == before
                assert await call("get_proactive_events") == []
                assert (
                    len(await call("get_proactive_events", consumer="desktop:other"))
                    == 1
                )

                # The next revision remains deliverable after the previous one was consumed.
                old_key = build_delivery_key(ctx)
                fixture["title"] = "再次更新的会议"
                assert (await call("poll_calendars", force=True))["ok"]
                ctx = await context()
                assert build_delivery_key(ctx) != old_key
                decision = await resolver.resolve(ctx)
                # Complete/cancel while waiting for a lane: the final guard blocks delivery.
                fixture["cancelled"] = True
                assert (await call("poll_calendars", force=True))["ok"]
                assert not await dispatch(decision.result)
                assert delivery.calls == before
                assert len(sessions.get_or_create(gateway.consumer).messages) == 1

                await registry.shutdown()
                tools = ToolRegistry()
                registry = McpServerRegistry(workspace / "mcp_servers.json", tools)
                await registry.load_and_connect_all()
                gateway = SharedMcpGateway(workspace, tools)
                gateway.consumer = "desktop:calendar-check"
                assert (await call("todo_list", status="completed"))["total"] == 1
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "checks": [
                                "installer_preserves_sources",
                                "stdio_mcp",
                                "three_channels_coexist",
                                "ics_http_sync",
                                "exact_revision_ack",
                                "consumer_isolation",
                                "wake_persistence",
                                "cancellation_revalidation",
                                "todo_completion",
                                "failed_send_retains_candidate",
                                "delivered_history",
                                "lost_ack_dedupe_retry",
                                "revision_delivery_key",
                                "lane_guard_rechecks",
                                "restart_persistence",
                            ],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            finally:
                await registry.shutdown()
                sessions._store.close()
                state.close()
                wake.close()
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--server-python", type=Path, required=True)
    args = parser.parse_args()
    if (
        not (args.backend / "agent/mcp/client.py").is_file()
        or not args.server_python.is_file()
    ):
        parser.error(
            "Provide Kaze's apps/backend and the prepared calendar Python environment"
        )
    asyncio.run(check(args.backend.resolve(), args.server_python.resolve()))


if __name__ == "__main__":
    main()
