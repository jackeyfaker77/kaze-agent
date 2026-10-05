import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bootstrap.proactive import run_proactive
from proactive_v2.config import ProactiveConfig


@pytest.mark.asyncio
@pytest.mark.parametrize("reply,deliver", [("NO_REPLY", False), ("", False), ("Reminder", True)])
async def test_heartbeat_reuses_session_and_delivers_only_actionable_reply(tmp_path, monkeypatch, reply, deliver):
    (tmp_path / "HEARTBEAT.md").write_text("Check reminders", encoding="utf-8")
    config = ProactiveConfig(enabled=True, session_key="daily", default_channel="telegram", default_chat_id="42")
    runtime = SimpleNamespace(config=SimpleNamespace(proactive=config),
        session_manager=SimpleNamespace(workspace=tmp_path),
        loop=SimpleNamespace(_active_tasks={}, process_direct=AsyncMock(return_value=reply)),
        push_tool=SimpleNamespace(execute=AsyncMock()))
    monkeypatch.setattr("bootstrap.proactive.asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()]))
    with pytest.raises(asyncio.CancelledError):
        await run_proactive(runtime)
    request = runtime.loop.process_direct.await_args.kwargs
    assert request["session_key"] == "daily"
    assert request["omit_user_turn"] and request["skip_post_memory"]
    assert request["disabled_tools"] == ["message_push"]
    if deliver:
        runtime.push_tool.execute.assert_awaited_once_with(channel="telegram", chat_id="42", message=reply, already_persisted=True)
    else:
        runtime.push_tool.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("busy,has_instructions", [(True, True), (False, False)])
async def test_heartbeat_skips_busy_sessions_or_missing_instructions(tmp_path, monkeypatch, busy, has_instructions):
    if has_instructions:
        (tmp_path / "HEARTBEAT.md").write_text("Check", encoding="utf-8")
    runtime = SimpleNamespace(config=SimpleNamespace(proactive=ProactiveConfig(enabled=True, session_key="daily")),
        session_manager=SimpleNamespace(workspace=tmp_path),
        loop=SimpleNamespace(_active_tasks={"daily": object()} if busy else {}, process_direct=AsyncMock()))
    monkeypatch.setattr("bootstrap.proactive.asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()]))
    with pytest.raises(asyncio.CancelledError):
        await run_proactive(runtime)
    runtime.loop.process_direct.assert_not_awaited()
