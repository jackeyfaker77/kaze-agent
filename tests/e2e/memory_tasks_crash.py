"""Real process exits over the production journal, provider and memory store."""
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'apps/backend')]
from tests.backend.memory2.test_durable_tasks import setup, admit, close
from memory2 import durable_tasks
from session.store.projection import ConversationProjection


async def main():
    workspace, point, phase = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    count = workspace / 'wire-count.txt'
    def wire():
        with count.open('a') as stream:
            stream.write('called\n')
            stream.flush()
            os.fsync(stream.fileno())
        if phase == 'crash' and point == 'wire':
            os._exit(73)
    f = await setup(workspace, wire_hook=wire)
    if phase == 'crash':
        def fault(where):
            if where == point:
                os._exit(73)
        durable_tasks.fault = fault
        if point == 'source_fact':
            original = ConversationProjection._after_fact_commit
            def after_fact(self):
                row = self.source.execute('SELECT payload FROM events ORDER BY seq DESC LIMIT 1').fetchone()
                if 'memory_post_response' in json.loads(row[0]).get('data', {}):
                    os._exit(73)
                original(self)
            ConversationProjection._after_fact_commit = after_fact
        await admit(f)
        await f.jobs.drain()
        raise AssertionError('crash point not reached')
    try:
        await f.jobs.drain()
        await f.jobs.drain()
        tasks = f.jobs.list_tasks()['tasks']
        print(json.dumps({'calls':len(count.read_text().splitlines()) if count.exists() else 0,
            'tasks': tasks,
            'items': f.store._db.execute('SELECT summary,status,reinforcement FROM memory_items ORDER BY summary').fetchall()}))
    finally:
        await close(f)

asyncio.run(main())
