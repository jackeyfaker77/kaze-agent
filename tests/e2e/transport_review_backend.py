"""Isolated backend fixture; no real model, accounts or Telegram requests."""
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'apps/backend'))
from agent.config_models import Config
from bootstrap.tools import build_core_runtime
from core.net.http import SharedHttpResources
from desktop_bridge.server import DesktopBridgeServer


async def main():
    workspace = Path(sys.argv[1])
    http = SharedHttpResources()
    runtime = build_core_runtime(Config(provider='openai', model='fixture', api_key='fixture',
                                       memory_optimizer_enabled=False), workspace, http)
    async def no_model(**kwargs):
        raise AssertionError('Queue review must not invoke the model')
    runtime.provider.chat = no_model
    store, box = runtime.bus.transport, runtime.bus.outbox
    journal = runtime.session_manager.execution_journal
    proactive = '--proactive' in sys.argv
    row, _ = journal.admit('desktop:queue-ui', 'proactive' if proactive else 'desktop', 'queue-ui', {})
    journal.start(row['turn_id'])
    if proactive:
        journal.append(row['turn_id'], 'proactive.stage.started', {'stage_id': 'judge', 'name': 'Judge'})
        journal.append(row['turn_id'], 'proactive.observed', {'stage': 'resolve', 'detail': {'decision': 'reply', 'evidence': ['calendar:one']}})
        journal.append(row['turn_id'], 'proactive.stage.completed', {'stage_id': 'judge'})
    journal.append(row['turn_id'], 'delivery.dispatched', {'effect_id': 'uncertain'})
    journal.finish(row['turn_id'], 'failed')
    box.accounts['fixture'] = 'original'
    box.handlers['bus:fixture'] = no_model
    key = box.enqueue('bus:fixture', {'channel': 'fixture', 'chat_id': '7', 'content': 'fixture'}, 'ui-unknown')
    store.link_execution(key, row['turn_id'])
    with store.db:
        store.db.execute("INSERT INTO parts VALUES (?, '1', '{}', 'done', '{\"message_id\":\"first\"}')", (key,))
        store.db.execute("INSERT INTO parts VALUES (?, '2', '{}', 'sending', NULL)", (key,))
    store.state(key, 'needs_review')
    server = DesktopBridgeServer(runtime)
    try:
        await server.serve_stdio()
    finally:
        await runtime.stop()
        await runtime.memory_runtime.aclose()
        runtime.session_manager._store.close()
        await http.aclose()


if __name__ == '__main__':
    asyncio.run(main())
