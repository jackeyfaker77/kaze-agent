"""Hard-exit probe with an fsynced fake platform receipt ledger."""

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from bus.chat_lane import ChatLane
from bus.durable import TransportStore, Outbox, DeliveryStopped

root, stage = Path(sys.argv[1]), sys.argv[2]
store = TransportStore(root)
queue = Outbox(store, ChatLane())


async def handler(payload):
    async def physical():
        with (root / "wire.jsonl").open("a") as stream:
            stream.write('{"message_id":123}\n')
            stream.flush()
            os.fsync(stream.fileno())
        if stage == "sending":
            os._exit(73)
        return SimpleNamespace(message_id=123)

    result = await queue.part("text", payload, physical)
    if stage == "receipt":
        os._exit(73)
    return {"message_id": str(result.message_id)}


async def main():
    queue.handlers["test"] = handler
    key = queue.enqueue(
        "test", {"channel": "test", "chat_id": "1", "text": "one"}, "stable"
    )
    if stage == "queued":
        os._exit(73)
    try:
        await queue.deliver(key)
    except DeliveryStopped:
        pass
    if stage == "done":
        os._exit(73)
    print(json.dumps({"state": store.get(key)["state"]}))


try:
    asyncio.run(main())
finally:
    store.close()
