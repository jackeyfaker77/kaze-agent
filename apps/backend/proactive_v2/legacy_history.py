"""将旧成功回执对应的现有助手消息标为主动历史，不创建或删除消息。"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import timedelta
from hashlib import sha256

from core.common.timekit import parse_iso

logger = logging.getLogger(__name__)


def classify_legacy_deliveries(sessions) -> int:
    directory = sessions.workspace / "proactive_deliveries"
    if not directory.exists():
        return 0
    changed = 0
    cache_updates = []
    with closing(sqlite3.connect(str(sessions.workspace / "sessions.db"))) as db:
        keys = [
            str(row[0]) for row in db.execute("SELECT key FROM sessions").fetchall()
        ]
        for key in keys:
            path = directory / f"{sha256(key.encode()).hexdigest()}.json"
            if not path.exists():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(
                payload.get("deliveries"), list
            ):
                raise ValueError(f"旧成功投递记录格式无效: {path}")
            rows = db.execute(
                "SELECT id, content, extra, ts FROM messages WHERE session_key = ? AND role = 'assistant'",
                (key,),
            ).fetchall()
            by_content = {}
            for message_id, content, extra, ts in rows:
                normalized = "\n".join(
                    line
                    for line in str(content).splitlines()
                    if not line.strip().startswith("USED:")
                ).strip()
                by_content.setdefault(normalized, []).append(
                    (message_id, extra, parse_iso(ts))
                )
            claimed = set()
            for delivery in payload["deliveries"]:
                if not isinstance(delivery, dict):
                    raise ValueError(f"旧成功投递记录必须是对象: {path}")
                sent_at = parse_iso(delivery.get("sent_at"))
                body = str(delivery.get("message") or "").strip()
                if sent_at is None or not body:
                    raise ValueError(f"旧成功投递记录缺少时间或正文: {path}")
                candidates = []
                for message_id, extra, created_at in by_content.get(body, []):
                    if (
                        message_id not in claimed
                        and created_at is not None
                        and abs(sent_at - created_at) <= timedelta(minutes=10)
                    ):
                        candidates.append(
                            (abs(sent_at - created_at), message_id, extra)
                        )
                if not candidates:
                    continue
                _, message_id, extra = min(candidates)
                claimed.add(message_id)
                metadata = json.loads(extra or "{}")
                if metadata.get("proactive"):
                    continue
                metadata.update(
                    proactive=True,
                    legacy_delivery_ref=str(delivery.get("delivery_ref") or ""),
                    evidence_item_ids=list(delivery.get("item_ids") or []),
                )
                db.execute(
                    "UPDATE messages SET extra = ? WHERE id = ?",
                    (json.dumps(metadata, ensure_ascii=False), message_id),
                )
                cache_updates.append((key, message_id, metadata))
                changed += 1
        db.commit()
    for key, message_id, metadata in cache_updates:
        cached = sessions._cache.get(key)
        if cached is not None:
            for message in cached.messages:
                if message.get("id") == message_id:
                    message.update(metadata)
    if changed:
        logger.info("[proactive] 已识别旧回执对应的成功主动消息: %d", changed)
    return changed
