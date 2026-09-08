"""Behavioral checks for session identity and shared memory."""
import asyncio
from types import SimpleNamespace

import pytest

from agent.config_models import Config
from agent.provider import LLMResponse
from bootstrap.tools import build_core_runtime
from core.memory.markdown.runtime import resolve_markdown_store
from core.memory.utils import resolve_memory_scope
from core.memory.engine import MemoryScope
from core.net.http import SharedHttpResources
from desktop_bridge.session_service import DesktopBridgeService
from bootstrap.session_migration import migrate_workspace
from core.memory.engine import MemoryMutation, MemoryToolSpec, MemoryMutationResult
from agent.tools.memorize import MemorizeTool
from core.channels.hub import ChannelHub
from bus.events import InboundMessage


def test_memory_is_global_without_identity(tmp_path):
    first = resolve_markdown_store(workspace=tmp_path)
    first.write_long_term("所有会话共享的事实")
    second = resolve_markdown_store(workspace=tmp_path, session_metadata={"session_key": "another"})
    assert "所有会话共享的事实" in second.read_long_term()
    assert first.memory_dir == tmp_path / "memory"
    assert not (tmp_path / "roles").exists()
    assert resolve_memory_scope(MemoryScope(session_key="chat-1")).session_key == "chat-1"


@pytest.mark.asyncio
async def test_plain_session_runs_complete_turn_and_persists(tmp_path):
    http = SharedHttpResources()
    runtime = build_core_runtime(Config(provider="openai", model="fake", api_key="fake"), tmp_path, http)
    calls = []
    async def chat(**kwargs):
        calls.append(kwargs["messages"])
        return LLMResponse(content="普通回复")
    runtime.provider.chat = chat
    try:
        reply = await runtime.loop.process_direct("你好", session_key="chat-123", channel="desktop", chat_id="chat-123", skip_post_memory=True)
        assert reply == "普通回复"
        assert runtime.session_manager.get_or_create("chat-123").messages[-1]["content"] == reply
        assert runtime.session_manager.get_or_create("other").messages == []
        assert calls
        assert not (tmp_path / "roles.json").exists()
        assert not (tmp_path / "roles").exists()
    finally:
        await runtime.stop()
        await runtime.memory_runtime.aclose()
        await http.aclose()


def test_channel_identity_is_transport_session(tmp_path):
    hub = ChannelHub(SimpleNamespace(), {"telegram": ["owner"]})
    assert hub.is_sender_allowed(channel="telegram", chat_id="42", sender_id="owner")
    assert not hub.is_sender_allowed(channel="telegram", chat_id="42", sender_id="stranger")
    message = InboundMessage(channel="telegram", chat_id="42", sender="owner", content="hi")
    assert hub.route_inbound(message).session_key == "telegram:42"
    assert hub.resolve_runtime_session_key("qq", "42") == "qq:42"


@pytest.mark.asyncio
async def test_memorize_accepts_session_without_character():
    requests = []
    class Memory:
        async def mutate(self, request):
            requests.append(request)
            return MemoryMutationResult(accepted=True, item_id="global-1", actual_kind="preference", status="new")
    tool = MemorizeTool(Memory(), MemoryToolSpec(description="save", parameters={}))
    await tool.execute("喜欢简洁回答", memory_kind="preference", channel="desktop", chat_id="a")
    assert requests[0].summary == "喜欢简洁回答"
    assert not hasattr(requests[0].scope, "role_id")


def test_workspace_migration_preserves_data_and_is_idempotent(tmp_path):
    import json
    import sqlite3
    from session.manager import SessionManager
    manager = SessionManager(tmp_path)
    old = manager.get_or_create("role:legacy")
    old.metadata.update(role_name="旧聊天", role_id="legacy")
    old.add_message("user", "保留这条消息")
    manager.save(old)
    manager._store.close()
    source = tmp_path / "roles" / "legacy" / "memory"
    source.mkdir(parents=True)
    (source / "MEMORY.md").write_text("旧的长期事实", encoding="utf-8")
    (source / "SELF.md").write_text("旧的虚构身份", encoding="utf-8")
    migrate_workspace(tmp_path)
    before = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    migrate_workspace(tmp_path)
    assert (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8") == before
    assert "旧的长期事实" in before
    assert not (tmp_path / "memory" / "SELF.md").exists()
    assert (source / "SELF.md").exists()
    assert (tmp_path / "memory" / "imports" / "legacy" / "memory" / "SELF.md").exists()
    with sqlite3.connect(tmp_path / "sessions.db") as db:
        key, metadata = db.execute("SELECT key, metadata FROM sessions").fetchone()
        assert key.startswith("desktop:imported-")
        assert "role_id" not in json.loads(metadata)
    assert (tmp_path / "migrations" / "before-plain-sessions-v1" / "sessions.db").exists()


@pytest.mark.asyncio
async def test_same_session_turns_serialize_without_merging_other_sessions(tmp_path):
    http = SharedHttpResources()
    runtime = build_core_runtime(Config(provider="openai", model="fake", api_key="fake"), tmp_path, http)
    active = 0
    max_active = 0
    async def chat(**kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(active, max_active)
        await asyncio.sleep(0.01)
        active -= 1
        return LLMResponse(content="ok")
    runtime.provider.chat = chat
    try:
        await asyncio.gather(*(runtime.loop.process_direct(str(n), session_key="same", skip_post_memory=True) for n in range(3)))
        assert max_active == 1
        assert len(runtime.session_manager.get_or_create("same").messages) == 6
        assert not runtime.loop._active_tasks
    finally:
        await runtime.stop()
        await runtime.memory_runtime.aclose()
        await http.aclose()


@pytest.mark.asyncio
async def test_bridge_create_get_and_delete_only_target_session(tmp_path):
    http = SharedHttpResources()
    runtime = build_core_runtime(Config(provider="openai", model="fake", api_key="fake"), tmp_path, http)
    bridge = DesktopBridgeService(runtime)
    try:
        for key in ("one", "two"):
            response = await bridge.handle({"method": "session.create", "payload": {"session_key": key}})
            assert response.error is None
            assert response.payload["session_key"] == key
        response = await bridge.handle({"method": "session.delete", "payload": {"session_key": "one"}})
        assert response.error is None
        remaining = runtime.session_manager.list_sessions()
        assert len(remaining) == 1
        assert not (tmp_path / "roles.json").exists()
    finally:
        await bridge.aclose()
        await runtime.stop()
        await runtime.memory_runtime.aclose()
        await http.aclose()
