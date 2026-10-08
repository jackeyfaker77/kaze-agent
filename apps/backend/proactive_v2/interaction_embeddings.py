"""为 Wake 提供 Kaze 已提交的普通互动向量；不写入对话正文。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from session.embedding_store import MessageEmbeddingStore


class InteractionEmbeddingCache:
    def __init__(self, workspace: Path) -> None:
        self._path = workspace / "sessions.db"
        self._store: MessageEmbeddingStore | None = None

    async def refresh(self, embedding_api: Any, *, now: datetime) -> int:
        model = str(getattr(embedding_api, "model_id", "") or "")
        embed = getattr(embedding_api, "embed_batch", None)
        if not model or not callable(embed) or not self._path.exists():
            return 0
        if self._store is None:
            self._store = MessageEmbeddingStore(self._path)
        # 仅从已提交的消息建立完整互动。待回复用户消息和主动消息不参与。
        with closing(sqlite3.connect(str(self._path))) as db:
            rows = db.execute(
                """
                SELECT id, session_key, seq, role, content, extra, julianday(ts)
                FROM messages
                WHERE role IN ('user', 'assistant') AND julianday(ts) <= julianday(?)
                ORDER BY julianday(ts) DESC, session_key, seq DESC LIMIT 1024
                """,
                (now.isoformat(),),
            ).fetchall()
        pending_user: dict[str, tuple[str, str]] = {}
        pairs: list[tuple[float, tuple[str, str], tuple[str, str]]] = []
        for message_id, key, seq, role, content, extra, ts in sorted(
            rows, key=lambda row: (row[1], row[2])
        ):
            metadata = json.loads(extra or "{}")
            if metadata.get("proactive") or not str(content or "").strip():
                continue
            message = (str(message_id), str(content))
            if role == "user":
                pending_user[key] = message
            elif key in pending_user:
                pairs.append((float(ts), pending_user.pop(key), message))
        missing = []
        for _, user, assistant in sorted(pairs, reverse=True)[:256]:
            pair = [
                item
                for item in (user, assistant)
                if self._store.get(message_id=item[0], content=item[1], model=model)
                is None
            ]
            if len(missing) + len(pair) > 64:
                break
            missing.extend(pair)
        if not missing:
            return 0
        embed_batch = cast(Callable[[list[str]], Awaitable[list[list[float]]]], embed)
        vectors = await embed_batch([content for _, content in missing])
        if len(vectors) != len(missing):
            raise ValueError("互动向量数量与已提交消息不一致")
        for (message_id, content), vector in zip(missing, vectors):
            self._store.upsert(
                message_id=message_id,
                content=content,
                model=model,
                embedding=list(vector),
            )
        return len(missing)

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None
