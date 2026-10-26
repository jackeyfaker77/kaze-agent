"""Durable SDK ingress and final Telegram sends; uncertain sends aren't replayed."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
from datetime import timedelta
from types import SimpleNamespace
from telegram import Update
from bus.durable import RetryDelivery, PermanentDelivery, delivery_scope

current_update: ContextVar[str | None] = ContextVar("telegram_update", default=None)


class PersistentUpdateQueue(asyncio.Queue):
    def __init__(self, store, namespace):
        super().__init__()
        self.store, self.namespace = store, namespace
        self.queued: set[str] = set()
        self.failed = False

    def key(self, update):
        return f"update:{self.namespace}:{update.update_id}"

    async def put(self, item):
        if not isinstance(item, Update):
            return await super().put(item)
        key = self.key(item)
        try:
            row = self.store.put(
                key, "telegram-update:" + self.namespace, json.loads(item.to_json())
            )
        except BaseException:
            self.failed = True
            raise
        import time

        if (
            row["state"] == "pending"
            and not row['review_closed']
            and row["due"] <= time.time()
            and key not in self.queued
        ):
            self.queued.add(key)
            await super().put(item)

    async def recover(self, bot):
        while True:
            for row in self.store.pending("telegram-update:" + self.namespace):
                if row["id"] not in self.queued:
                    await self.put(Update.de_json(row["payload"], bot))
            await asyncio.sleep(0.25)


class _DurableMixin:
    async def _on_durable_update(self, update, context):
        queue = self._durable_updates
        raw_key = queue.key(update)
        if queue.store.get(raw_key)['review_closed']:
            queue.queued.discard(raw_key)
            return
        queue.store.active.add(raw_key)
        msg = update.effective_message
        identity = (
            f"in:{queue.namespace}:{msg.chat_id}:{msg.message_id}" if msg else raw_key
        )
        token = current_update.set(identity)
        try:
            known = queue.store.db.execute(
                "SELECT 1 FROM jobs WHERE id=?", (identity,)
            ).fetchone()
            if known is None and msg:
                text = msg.text or ""
                command = text.split(" ", 1)[0].split("@", 1)[0]
                if command == "/stop":
                    await self._on_stop_command(update, context)
                elif text.startswith("/"):
                    await self._on_command(update, context)
                elif msg.text:
                    await self._on_message(update, context)
                elif msg.photo:
                    await self._on_photo(update, context)
                elif msg.document:
                    await self._on_document(update, context)
            queue.store.state(raw_key, "done")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            row = queue.store.get(raw_key)
            queue.store.attempt(raw_key)
            queue.store.state(
                raw_key,
                "failed" if row["attempts"] >= 4 else "pending",
                error=type(exc).__name__,
                delay=min(2 ** row["attempts"], 60),
            )
        finally:
            queue.store.active.discard(raw_key)
            current_update.reset(token)
            queue.queued.discard(raw_key)

    async def _durable_send(self, method: str, chat_id: str, **args):
        outbox = self._bus.outbox
        cid = self._resolve_chat_id(chat_id)
        if delivery_scope.get() is None:
            payload = {
                "channel": self._channel,
                "chat_id": cid,
                "method": method,
                "args": args,
            }
            identity = current_update.get()
            key = outbox.enqueue(
                "telegram:" + self._channel,
                payload,
                "command:" + identity if identity else None,
            )
            if identity:
                # /stop must not hold up SDK shutdown while a durable send backs off.
                return None
            result = await self._bus.chat_lane.run_send(
                self._channel, cid, lambda: outbox.deliver(key)
            )
            return SimpleNamespace(message_id=result["message_id"])
        parts = []
        scope = delivery_scope.get()
        ordinal = scope.sequence
        scope.sequence += 1
        if method == "text":
            from infra.channels.telegram_utils import convert_with_segments
            from telegramify_markdown.entity import split_entities
            from infra.channels.telegram_utils.sending import (
                _strip_chunk,
                _serialize_entities,
            )

            try:
                text, entities, _ = convert_with_segments(args["text"])
                for text, entities in split_entities(text, entities, 4090):
                    text, entities = _strip_chunk(text, entities)
                    if text:
                        parts.append(
                            {"text": text, "entities": _serialize_entities(entities)}
                        )
            except Exception:
                chunk = ""
                units = 0
                for char in args["text"]:
                    size = len(char.encode("utf-16-le")) // 2
                    if units + size > 4090:
                        parts.append({"text": chunk})
                        chunk = ""
                        units = 0
                    chunk += char
                    units += size
                if chunk:
                    parts.append({"text": chunk})
        else:
            parts = [args]
        last = None
        for index, part in enumerate(parts):
            # Keep the logical part identity stable across renderer upgrades. A
            # changed payload must be fenced by Outbox.part, never become a new send.
            name = f"{ordinal}:{method}:{index}"

            async def physical(part=part):
                from telegram.error import (
                    BadRequest,
                    Forbidden,
                    InvalidToken,
                    RetryAfter,
                )

                try:

                    async def send():
                        if method == "text":
                            return await self._app.bot.send_message(
                                chat_id=int(cid), **part
                            )
                        if method == "file":
                            return await self._send_document_file(
                                int(cid),
                                part["path"],
                                part.get("name"),
                                part.get("caption"),
                            )
                        if part["image"].startswith(("http://", "https://")):
                            return await self._app.bot.send_photo(
                                chat_id=int(cid), photo=part["image"]
                            )
                        return await self._send_photo_file(int(cid), part["image"])

                    return await self._telegram_outbound_limiter.run(
                        cid,
                        kind="send",
                        label="durable_send",
                        action=send,
                        max_attempts=1,
                    )
                except RetryAfter as exc:
                    delay = exc.retry_after
                    seconds = (
                        delay.total_seconds()
                        if isinstance(delay, timedelta)
                        else float(delay)
                    )
                    raise RetryDelivery(seconds) from exc
                except (BadRequest, Forbidden, InvalidToken, FileNotFoundError) as exc:
                    raise PermanentDelivery() from exc
                except Exception as exc:
                    import httpx

                    cause = exc
                    while cause is not None:
                        if isinstance(
                            cause,
                            (
                                httpx.ConnectError,
                                httpx.ConnectTimeout,
                                httpx.PoolTimeout,
                            ),
                        ):
                            raise RetryDelivery() from exc
                        cause = cause.__cause__
                    raise

            last = await outbox.part(name, {"chat_id": cid, **part}, physical)
        return last

    async def _deliver_command(self, payload):
        result = await self._durable_send(
            payload["method"], payload["chat_id"], **payload["args"]
        )
        return {"message_id": getattr(result, "message_id", None)}
