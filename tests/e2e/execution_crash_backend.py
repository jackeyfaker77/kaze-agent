"""Real os._exit faults: no finally, no graceful shutdown, no mocked crash."""
import asyncio
import json
import os
from pathlib import Path
import sys

from bus.events import InboundMessage, OutboundMessage
from core.execution.contracts import ExecutionUnavailable
from core.execution.delivery import record_delivery
from core.execution.journal import ExecutionJournal
from core.execution.runner import run_recorded
from core.execution.scope import current_execution
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry

workspace, mode = Path(sys.argv[1]), sys.argv[2]
journal = ExecutionJournal(workspace)
item = InboundMessage("desktop", "user", "test", "original input", media=["original.png"],
                      metadata={"execution_request_id": "crash-test"})


def effect():
    with (workspace / "effects.txt").open("a", encoding="utf-8") as stream:
        stream.write("effect\n")
        stream.flush()
        os.fsync(stream.fileno())


class CrashTool(Tool):
    name = "crash_tool"
    description = "Isolated crash probe"
    parameters = {"type": "object", "properties": {}}
    async def execute(self, **kwargs):
        if mode == "tool_before":
            os._exit(73)
        effect()
        if mode == "tool_after":
            os._exit(73)
        return "tool result"


async def operation():
    scope = current_execution.get()
    assert scope is not None
    if mode == "partial":
        scope.record("reply.delta", {"content": "saved partial", "thinking": "saved reasoning"})
        os._exit(73)
    if mode.startswith("tool_"):
        registry = ToolRegistry()
        registry.register(CrashTool())
        await registry.execute("crash_tool", {})
        os._exit(73)
    if mode == "delivery_after":
        async def send():
            effect()
            os._exit(73)
        await record_delivery(journal, item.session_key, {"channel": "fake", "content": "reminder"}, send)
    return OutboundMessage("desktop", "test", "confirmed reply")


async def main():
    if mode == "recover":
        async def forbidden():
            raise AssertionError("Recovery must never execute a model or tool")
        try:
            await run_recorded(journal, item, item.session_key, forbidden)
        except ExecutionUnavailable:
            pass
        row = journal.find(item.session_key, "desktop", "crash-test")
        assert row is not None
        events = journal.events(row["turn_id"])["events"]
        row["partial"] = "".join(e["payload"].get("content", "") for e in events if e["kind"] == "reply.delta")
        print(json.dumps(row))
        journal.close()
        return
    if mode == "accepted":
        start = journal.start
        def crash_start(turn):
            os._exit(73)
        journal.start = crash_start
    await run_recorded(journal, item, item.session_key, operation)
    os._exit(73)


asyncio.run(main())
