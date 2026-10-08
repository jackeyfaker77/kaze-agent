"""Session-scoped adapter from declared MCP sources to the ordinary Agent."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from proactive_v2.gateway import DataGateway, GatewayResult
from proactive_v2.legacy_mcp_sources import (
    McpClientPool,
    _extract_context_items,
    _load_sources,
)

logger = logging.getLogger(__name__)


def item_id(item: dict) -> str:
    return str(item.get("item_id") or item.get("event_id") or "")


@dataclass
class SourceBatch:
    source_ok: bool
    candidates: list[dict] = field(default_factory=list)
    data: GatewayResult = field(default_factory=GatewayResult)
    disabled_tools: list[str] = field(default_factory=list)


class ProactiveSources:
    """Own persistent connections and one immutable source snapshot per tick.

    No declaration file returns None so the host can use its legacy Feed adapter.
    An explicit empty/disabled declaration never falls back to an undeclared source.
    """

    def __init__(self, workspace: Path, *, web_fetch_tool=None, pool=None) -> None:
        self.workspace = workspace
        self.pool = pool or McpClientPool(workspace)
        self.web_fetch_tool = web_fetch_tool

    async def close(self) -> None:
        await self.pool.disconnect_all()

    async def fetch(self, consumer: str, *, limit: int = 20) -> SourceBatch | None:
        if not (self.workspace / "proactive_sources.json").exists():
            # Also close old connections after a declaration is removed.
            await self.pool.disconnect_all()
            return None
        sources = _load_sources(self.workspace)
        await self.pool.connect_all()
        routes: dict[tuple[str, str, str], list[dict]] = {}

        async def read_source(group: list[dict], tool: str):
            source = group[0]
            server = source["server"]
            polls = {
                (
                    item["poll_tool"],
                    json.dumps(item.get("poll_args", {}), sort_keys=True),
                )
                for item in group
                if item.get("poll_tool")
            }
            for poll_tool, encoded_args in sorted(polls):
                try:
                    await self.pool.call(
                        server,
                        poll_tool,
                        json.loads(encoded_args),
                        timeout=180,
                    )
                except Exception:
                    logger.warning(
                        "[proactive.sources] poll failed server=%s",
                        server,
                        exc_info=True,
                    )
            return await self.pool.call(
                server,
                tool,
                source.get("get_args", {}),
                retry_on_transport=True,
                optional_args={"consumer": consumer, "limit": limit},
            )

        for source in sources:
            server = source["server"]
            tool = source.get("get_tool") or (
                "get_context"
                if source["channel"] == "context"
                else "get_proactive_events"
            )
            args = source.get("get_args", {})
            route = (server, tool, json.dumps(args, sort_keys=True))
            routes.setdefault(route, []).append(source)
        requests = {
            route: asyncio.create_task(read_source(group, route[1]))
            for route, group in routes.items()
        }
        try:
            # Sources on different servers execute concurrently. A server's stdio
            # transport is serialized by the pool; shared get_tool is read once.
            values = await asyncio.gather(*requests.values(), return_exceptions=True)
        except BaseException:
            for task in requests.values():
                task.cancel()
            await asyncio.gather(*requests.values(), return_exceptions=True)
            raise
        payloads = dict(zip(requests, values))
        alerts: list[dict] = []
        content: list[dict] = []
        context: list[dict] = []
        succeeded = 0
        disabled: list[str] = []
        for source in sources:
            server = source["server"]
            channel = source["channel"]
            tool = source.get("get_tool") or (
                "get_context" if channel == "context" else "get_proactive_events"
            )
            ack_tool = source.get("ack_tool") or "acknowledge_events"
            disabled.extend(
                f"mcp_{server}__{name}"
                for name in (tool, ack_tool, source.get("poll_tool"))
                if name
            )
            raw = payloads[
                (server, tool, json.dumps(source.get("get_args", {}), sort_keys=True))
            ]
            try:
                if isinstance(raw, BaseException):
                    raise RuntimeError("source call failed") from raw
                if isinstance(raw, dict) and raw.get("error"):
                    raise ValueError("source returned an error")
                if channel == "context":
                    context.extend(_extract_context_items(raw, server=server))
                else:
                    events = raw
                    if isinstance(raw, dict):
                        events = raw.get("items", raw.get("events"))
                    if not isinstance(events, list):
                        raise ValueError(
                            "event source must return a list or items/events envelope"
                        )
                    for event in events:
                        if not isinstance(event, dict):
                            continue
                        kind = event.get("kind") or channel
                        if kind not in {"alert", "content"} or (
                            channel and kind != channel
                        ):
                            continue
                        event_id = str(
                            event.get("event_id") or event.get("id") or ""
                        ).strip()
                        if not event_id:
                            continue
                        target = alerts if kind == "alert" else content
                        target.append(
                            {
                                **event,
                                "event_id": event_id,
                                "kind": kind,
                                "ack_server": server,
                                "ack_tool": ack_tool,
                                "ack_args": source.get("ack_args", {}),
                                "item_id": f"{server}:{event_id}",
                            }
                        )
                succeeded += 1
            except Exception:
                logger.warning(
                    "[proactive.sources] fetch failed server=%s tool=%s",
                    server,
                    tool,
                    exc_info=True,
                )

        async def alert_fn():
            return list({item_id(item): item for item in alerts}.values())[:limit]

        async def feed_fn(limit: int):
            return list({item_id(item): item for item in content}.values())[:limit]

        async def context_fn():
            return context

        data = await DataGateway(
            alert_fn=alert_fn,
            feed_fn=feed_fn,
            context_fn=context_fn,
            web_fetch_tool=self.web_fetch_tool,
        ).run()
        candidates = data.alerts + [
            {**meta, "item_id": meta["id"]} for meta in data.content_meta
        ]
        # A declaration with no enabled sources is a valid empty snapshot.
        return SourceBatch(
            bool(succeeded) or not sources,
            candidates,
            data,
            list(dict.fromkeys(disabled)),
        )

    async def acknowledge(
        self, consumer: str, items: list[dict], *, reason: str, delivery_ref: str = ""
    ) -> None:
        grouped: dict[tuple[str, str, str], list[str]] = {}
        for item in items:
            server = item["ack_server"]
            tool = item["ack_tool"]
            route = (server, tool, json.dumps(item.get("ack_args", {}), sort_keys=True))
            grouped.setdefault(route, []).append(item["event_id"])
        for (server, tool, encoded_args), ids in grouped.items():
            try:
                result: Any = await self.pool.call(
                    server,
                    tool,
                    {**json.loads(encoded_args), "event_ids": list(dict.fromkeys(ids))},
                    optional_args={
                        "consumer": consumer,
                        "reason": reason,
                        "delivery_ref": delivery_ref,
                        "ttl_hours": 24,
                    },
                )
                if (
                    isinstance(result, dict)
                    and (result.get("error") or result.get("ok") is False)
                ) or (isinstance(result, str) and result.lower().startswith("error:")):
                    raise RuntimeError("source rejected ACK")
            except Exception:
                logger.warning(
                    "[proactive.sources] ACK failed server=%s tool=%s",
                    server,
                    tool,
                    exc_info=True,
                )
