import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from proactive_v2.source_refresh import SourceRefresher


@pytest.mark.asyncio
async def test_declared_refresh_is_deduplicated_and_cancellable(tmp_path):
    source = {
        "server": "news",
        "get_tool": "cached",
        "poll_tool": "refresh",
        "poll_args": {"topic": "dev"},
    }
    (tmp_path / "proactive_sources.json").write_text(
        json.dumps(
            {
                "sources": [
                    {**source, "channel": "content"},
                    {**source, "channel": "content"},
                    {**source, "server": "other", "channel": "content"},
                ]
            }
        ),
        encoding="utf-8",
    )
    gateway = AsyncMock()
    refresher = SourceRefresher(
        tmp_path, gateway, active_routes={("news", "cached")}, interval_seconds=150
    )
    await refresher.refresh_once()
    gateway.call.assert_awaited_once_with(
        "news", "refresh", {"topic": "dev"}, timeout=180
    )
    refresher.start()
    await asyncio.sleep(0)
    await refresher.aclose()
    assert refresher._task is None
