import json
import pytest

from agent.core.proactive_turn.gates import (
    ProactiveGateAdapter,
    ProactiveGateDecision,
    ProactiveMode,
)
from bootstrap.proactive import _builtin_contributions
from tests.backend.bootstrap import test_proactive as proactive_fixtures
from tests.backend.bootstrap.test_proactive import (
    KEY,
    NOW,
    SourceTool,
    structured,
)

proactive_setup = proactive_fixtures.proactive_setup


class Gate(ProactiveGateAdapter):
    name = "test_gate"
    priority = 10

    def __init__(self, decision):
        self.decision = decision
        self.completions = []

    def evaluate(self, ctx):
        return self.decision

    def finalize(self, completion):
        self.completions.append(completion)


def manager(gate):
    from types import SimpleNamespace

    return SimpleNamespace(
        **_builtin_contributions(),
        proactive_gates=[gate],
        proactive_sources=[],
        drift_skill_roots=[],
        tool_hooks=[],
    )


@pytest.mark.asyncio
async def test_rules_reloaded_every_tick(proactive_setup):
    setup = proactive_setup
    loop = await setup.start()
    rules = setup.workspace / "PROACTIVE_CONTEXT.md"
    for rule in ("只在白天联系", "只推紧急事项"):
        rules.write_text(rule, encoding="utf-8")
        setup.fetch.return_value[0]["event_id"] = rule
        await loop._tick()
        assert rule in json.dumps(
            setup.runtime.provider.chat.await_args.kwargs["messages"],
            ensure_ascii=False,
        )


@pytest.mark.asyncio
async def test_plugin_block_prevents_fetch_and_model(proactive_setup):
    setup = proactive_setup
    setup.runtime.plugin_manager = manager(Gate(ProactiveGateDecision.block("pause")))
    loop = await setup.start()
    await loop._tick()
    setup.fetch.assert_not_awaited()
    setup.runtime.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_activated_plugin_is_finalized_once_with_real_delivery_outcome(
    proactive_setup, fail
):
    from agent.tools.message_push import DeliveryReceipt

    setup = proactive_setup
    gate = Gate(
        ProactiveGateDecision.activate(ProactiveMode.HEARTBEAT, reason="用户要求的提醒")
    )
    setup.runtime.plugin_manager = manager(gate)
    if fail:
        setup.sender.return_value = DeliveryReceipt(
            False, "telegram", "42", "failed", error="offline"
        )
    loop = await setup.start()
    await loop._tick()
    assert len(gate.completions) == 1
    assert gate.completions[0].outcome == ("closed" if fail else "delivered")
    assert "用户要求的提醒" in json.dumps(
        setup.runtime.provider.chat.await_args.kwargs["messages"], ensure_ascii=False
    )


@pytest.mark.asyncio
async def test_unreadable_rules_stop_model_and_delivery(proactive_setup, monkeypatch):
    from pathlib import Path

    setup = proactive_setup
    loop = await setup.start()
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path.name == "PROACTIVE_CONTEXT.md":
            raise PermissionError("rules unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(PermissionError):
        await loop._tick()
    setup.runtime.provider.chat.assert_not_awaited()
    setup.sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_sources_failing_does_not_trigger_drift(proactive_setup):
    setup = proactive_setup
    setup.runtime.config.proactive.drift_enabled = True
    setup.fetch.side_effect = RuntimeError("unavailable")
    loop = await setup.start()
    with pytest.raises(RuntimeError, match="拉取失败"):
        await loop._tick()
    setup.runtime.provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_uses_structured_candidate_pipeline(proactive_setup):
    setup = proactive_setup
    setup.runtime.config.proactive.lifecycle = "default"
    setup.runtime.provider.chat.side_effect = [
        structured("get_alert_events"),
        structured("message_push", message="提醒：该处理这件事了。", evidence=[]),
        structured("finish_turn", decision="reply"),
    ]
    loop = await setup.start()
    await loop._tick()
    setup.runtime.loop.process_direct.assert_not_awaited()
    assert setup.runtime.provider.chat.await_count >= 2
    setup.sender.assert_awaited_once()
    assert setup.sessions.get_or_create(KEY).messages[-1]["proactive"]


@pytest.mark.asyncio
async def test_wake_content_is_locally_owned_before_investigation_and_share(
    proactive_setup,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    setup = proactive_setup
    (setup.workspace / "proactive_sources.json").write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "server": "not_feed",
                        "channel": "content",
                        "get_tool": "fetch",
                        "ack_tool": "ack",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    setup.fetch.return_value = [
        {
            "kind": "content",
            "event_id": "article",
            "title": "新的编程工具",
            "published_at": NOW.isoformat(),
            "preprocess_score": 0.9,
            "url": "https://example.com/article",
        }
    ]
    fetch_body = AsyncMock(
        return_value={
            "text": "这是一篇文章的正文",
            "url": "https://example.com/article",
        }
    )
    setup.runtime.tools.register(SourceTool("web_fetch", fetch_body))
    setup.runtime.provider.chat.side_effect = [
        structured(
            "scratchpad",
            items=[
                {"item_id": "candidate_1", "initial_interest": "likely_interesting"}
            ],
        ),
        structured(
            "share_content",
            opening="这个工具值得留意。",
            items=[
                {
                    "item_id": "candidate_1",
                    "summary": "新的编程工具已经发布",
                    "why_it_matters": "能用于日常开发",
                }
            ],
        ),
    ]
    rng = SimpleNamespace(random=lambda: 0.000001, gammavariate=lambda *_: 0.000001)
    loop = await setup.start(rng)
    await loop._tick()
    setup.ack.assert_awaited_once()
    fetch_body.assert_awaited_once()
    setup.sender.assert_awaited_once()
    message = setup.sessions.get_or_create(KEY).messages[-1]
    assert "编程工具" in message["content"]
    assert message["source_refs"][0]["url"] == "https://example.com/article"
    assert message["evidence_item_ids"][0].endswith(":article")
    await loop._tick()
    assert setup.runtime.provider.chat.await_count == 2
    assert setup.sender.await_count == 1


@pytest.mark.asyncio
async def test_default_sqlite_delivery_dedupe_survives_restart(proactive_setup):
    setup = proactive_setup
    setup.runtime.config.proactive.lifecycle = "default"
    setup.runtime.config.proactive.agent_tick_delivery_cooldown_hours = 0
    decisions = [
        structured("get_alert_events"),
        structured("message_push", message="处理这件事", evidence=[]),
        structured("finish_turn", decision="reply"),
    ]
    setup.runtime.provider.chat.side_effect = decisions * 2
    first = await setup.start()
    await first._tick()
    await setup.stop(first)
    second = await setup.start()
    await second._tick()
    assert setup.sender.await_count == 1
    assert len(setup.sessions.get_or_create(KEY).messages) == 1


@pytest.mark.asyncio
async def test_default_semantic_guard_reads_actual_sent_history(proactive_setup):
    from agent.provider import LLMResponse

    setup = proactive_setup
    cfg = setup.runtime.config.proactive
    (
        cfg.lifecycle,
        cfg.agent_tick_delivery_cooldown_hours,
        cfg.message_dedupe_enabled,
    ) = ("default", 0, True)
    setup.runtime.provider.chat.side_effect = [
        structured("get_alert_events"),
        structured("message_push", message="记得处理这个待办", evidence=[]),
        structured("finish_turn", decision="reply"),
        structured("get_alert_events"),
        structured("message_push", message="这件待办还没处理，提醒一下", evidence=[]),
        structured("finish_turn", decision="reply"),
        LLMResponse(
            content=json.dumps({"is_duplicate": True, "reason": "相同待办，无新信息"})
        ),
    ]
    loop = await setup.start()
    await loop._tick()
    await loop._tick()
    setup.sender.assert_awaited_once()
    request = setup.runtime.provider.chat.await_args.kwargs
    assert "记得处理这个待办" in json.dumps(request["messages"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_wake_drift_due_time_enters_full_skill_pipeline(proactive_setup):
    from datetime import timedelta
    from types import SimpleNamespace

    setup = proactive_setup
    setup.fetch.return_value = []
    setup.runtime.config.proactive.drift_enabled = True
    folder = setup.workspace / "drift/skills/explore-curiosity"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: explore-curiosity\ndescription: 探索一个轻量想法\n---\n",
        encoding="utf-8",
    )
    setup.runtime.provider.chat.side_effect = [
        structured(
            "select_skill",
            skill_name="explore-curiosity",
            decision="explore",
            intention="想想事件钟",
            reason="适合轻量探索",
        ),
        structured("message_push", message="时间回放可以做成事件钟。"),
        structured(
            "finish_drift",
            skill_used="explore-curiosity",
            status="completed",
            briefing="分享了事件钟想法",
            self_update={
                "next_tendency": "下次按状态选择",
                "reflection": "本轮完成",
                "pattern": "ordinary",
            },
        ),
    ]
    loop = await setup.start(
        SimpleNamespace(random=lambda: 0.000001, gammavariate=lambda *_: 0.000001)
    )
    await loop._tick()
    setup.runtime.provider.chat.assert_not_awaited()
    setup.clock.now = lambda: NOW + timedelta(minutes=1)
    await loop._tick()
    assert setup.runtime.provider.chat.await_count == 3
    setup.sender.assert_awaited_once()
    assert (
        setup.sessions.get_or_create(KEY).messages[-1]["content"]
        == "时间回放可以做成事件钟。"
    )
    assert (setup.workspace / "drift/drift.db").exists()


@pytest.mark.asyncio
async def test_real_plugin_manager_provides_both_lifecycles(proactive_setup):
    from pathlib import Path
    from agent.plugins.manager import PluginManager

    setup = proactive_setup
    plugins = Path(__file__).resolve().parents[3] / "apps/backend/plugins"
    manager = PluginManager(
        [plugins],
        event_bus=setup.runtime.event_bus,
        tool_registry=setup.runtime.tools,
        workspace=setup.workspace,
    )
    names = {
        "default_proactive",
        "proactive_flow",
        "drift_flow",
        "wake_proactive",
        "wake_proactive_flow",
        "wake_drift_flow",
    }
    for entry in manager.discover():
        if entry["name"] in names:
            await manager._load_one(entry)
    assert manager.loaded_count == 6
    assert {life.id for life in manager.proactive_lifecycles} == {"default", "wake"}
    setup.runtime.plugin_manager = manager
    loop = None
    try:
        loop = await setup.start()
        await loop._tick()
        setup.sender.assert_awaited_once()
    finally:
        if loop is not None:
            await setup.stop(loop)
        await manager.terminate_all()
