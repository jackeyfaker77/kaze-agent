from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from proactive_v2.interaction_embeddings import InteractionEmbeddingCache
from session.embedding_store import MessageEmbeddingStore
from session.store import SessionStore


@pytest.mark.asyncio
async def test_only_committed_passive_pairs_are_embedded_and_cached(tmp_path):
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    sessions = SessionStore(tmp_path / "sessions.db")
    sessions.create_session(key="desktop:one", metadata={})
    for seq, (role, content, extra, ts) in enumerate(
        [
            ("user", "我关注编程工具", {}, now - timedelta(hours=2)),
            ("assistant", "可以从编辑器说起", {}, now - timedelta(hours=1)),
            ("user", "还没得到回复", {}, now),
            ("assistant", "已经发过的主动消息", {"proactive": True}, now),
            ("assistant", "未来的回复", {}, now + timedelta(hours=1)),
        ]
    ):
        sessions.insert_message(
            "desktop:one",
            role=role,
            content=content,
            extra=extra,
            ts=ts.isoformat(),
            seq=seq,
        )
    api = SimpleNamespace(
        model_id="fake-model",
        embed_batch=AsyncMock(side_effect=lambda texts: [[1.0, 0.0] for _ in texts]),
    )
    cache = InteractionEmbeddingCache(tmp_path)
    try:
        assert await cache.refresh(api, now=now) == 2
        assert set(api.embed_batch.await_args.args[0]) == {
            "我关注编程工具",
            "可以从编辑器说起",
        }
        assert await cache.refresh(api, now=now) == 0
        api.embed_batch.assert_awaited_once()
        vectors = MessageEmbeddingStore(tmp_path / "sessions.db")
        try:
            assert (
                len(vectors.list_until(model="fake-model", cutoff=now.isoformat())) == 2
            )
        finally:
            vectors.close()
    finally:
        cache.close()
        sessions.close()
