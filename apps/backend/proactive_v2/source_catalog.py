"""将旧工作区声明编译成与插件声明相同的主动源目录。"""

import json
from pathlib import Path
from hashlib import sha256

from agent.plugins.specs import ProactiveSourceSpec, RegisteredProactiveSource
from proactive_v2.legacy_mcp_sources import _load_sources


def workspace_sources(workspace: Path) -> list[RegisteredProactiveSource]:
    """读取旧声明并合并使用同一 fetch/ack 路由的通道。"""
    grouped: dict[tuple[str, str, str, str, str], dict] = {}

    for source in _load_sources(workspace):
        server = source["server"]
        fetch = str(source.get("get_tool") or source.get("fetch_tool") or "")
        if not fetch:
            raise ValueError(f"工作区主动源缺少 get_tool: {server}")
        ack = str(source.get("ack_tool") or "")
        args = dict(source.get("get_args", {}))
        route = (
            server,
            fetch,
            ack,
            json.dumps(args, sort_keys=True),
            json.dumps(source.get("ack_args", {}), sort_keys=True),
        )
        item = grouped.setdefault(
            route,
            {"channels": [], "args": args, "ack_args": source.get("ack_args", {})},
        )
        channels = (
            (source["channel"],)
            if source["channel"]
            else ("alert", "content", "context")
        )
        item["channels"].extend(
            channel for channel in channels if channel not in item["channels"]
        )
    result = []
    for route, item in grouped.items():
        server, fetch, ack, _, _ = route
        digest = sha256(json.dumps(route).encode()).hexdigest()[:12]
        result.append(
            RegisteredProactiveSource(
                plugin_id="workspace",
                spec=ProactiveSourceSpec(
                    id=f"{server}-{fetch}-{digest}",
                    channels=tuple(item["channels"]),
                    server=server,
                    fetch_tool=fetch,
                    ack_tool=ack,
                    fetch_args=item["args"],
                    ack_args=dict(item["ack_args"]),
                ),
            )
        )
    return result
