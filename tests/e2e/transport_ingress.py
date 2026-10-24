"""Isolate the real SDK from legacy channel tests that stub sys.modules."""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from telegram import Update
from telegram.error import Forbidden, RetryAfter
from bus.chat_lane import ChatLane
from bus.durable import TransportStore, Outbox, DeliveryStopped
from infra.channels.telegram_channel.durable import PersistentUpdateQueue, _DurableMixin
from infra.channels.telegram_utils import TelegramOutboundLimiter


async def verify_segmented_delivery(store):
    class Harness(_DurableMixin):
        def _resolve_chat_id(self, chat_id):
            return chat_id
    harness = Harness()
    outbox = Outbox(store, ChatLane())
    harness._bus = SimpleNamespace(outbox=outbox)
    harness._channel = 'telegram'
    harness._telegram_outbound_limiter = TelegramOutboundLimiter()
    calls, accepted = [], []
    async def send_message(chat_id, text, **kwargs):
        calls.append(text)
        if len(calls) == 2:
            raise RetryAfter(0)
        assert len(text.encode('utf-16-le')) // 2 <= 4090
        accepted.append(text)
        return SimpleNamespace(message_id=len(accepted))
    harness._app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
    outbox.handlers['telegram:telegram'] = harness._deliver_command
    content = '😀' * 3000
    payload = {'channel':'telegram', 'chat_id':'1', 'method':'text', 'args':{'text':content}}
    key = outbox.enqueue('telegram:telegram', payload, 'segmented')
    await outbox.deliver(key)
    await outbox.deliver(key)
    assert len(calls) == 3 and len(accepted) == 2 and ''.join(accepted) == content
    assert store.get(key)['state'] == 'done'
    async def forbidden(**kwargs):
        raise Forbidden('blocked')
    harness._app.bot.send_message = forbidden
    key = outbox.enqueue('telegram:telegram', payload, 'forbidden')
    try:
        await outbox.deliver(key)
    except DeliveryStopped:
        pass
    else:
        raise AssertionError('Permanent rejection must stop delivery')
    assert store.get(key)['state'] == 'failed' and store.get(key)['attempts'] == 1


async def main():
    store = TransportStore(Path(sys.argv[1]))
    queue = PersistentUpdateQueue(store, "telegram:bot")
    try:
        update = Update.de_json(
            {
                "update_id": 100,
                "message": {
                    "message_id": 10,
                    "date": 1700000000,
                    "chat": {"id": 1, "type": "private"},
                    "from": {"id": 1, "is_bot": False, "first_name": "Test"},
                    "text": "hello",
                },
            },
            None,
        )
        await queue.put(update)
        assert store.get("update:telegram:bot:100")["state"] == "pending"
        await queue.put(update)
        assert queue.qsize() == 1
        store.db.execute(
            "CREATE TRIGGER fail_insert BEFORE INSERT ON jobs BEGIN SELECT RAISE(FAIL,'disk unavailable'); END"
        )
        try:
            await queue.put(Update.de_json({"update_id": 101}, None))
        except Exception as exc:
            assert "disk unavailable" in str(exc)
        else:
            raise AssertionError(
                "Failed durable write must stop SDK offset advancement"
            )
        assert queue.failed and queue.qsize() == 1
        store.db.execute('DROP TRIGGER fail_insert')
        await verify_segmented_delivery(store)
    finally:
        store.close()


asyncio.run(main())
