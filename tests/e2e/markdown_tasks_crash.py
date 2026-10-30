"""Fresh-process recovery through production files, provider, journal and queue."""
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'apps/backend')]
from tests.backend.core.memory.markdown.test_durable import setup, admit, close
from core.memory.markdown import durable
from memory2 import durable_tasks
from session.store.projection import ConversationProjection
from core.memory.task_context import memory_task


async def main():
    workspace, point, phase = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    counter = workspace / 'wire.txt'
    def wire():
        with counter.open('a') as stream:
            stream.write('call\n')
            stream.flush()
            os.fsync(stream.fileno())
        count = len(counter.read_text().splitlines())
        if phase == 'crash' and ((point == 'wire' and count == 1) or (point == 'wire_second' and count == 2) or (point == 'callback_wire' and count == 3)):
            os._exit(73)
    callback = point.startswith('callback_')
    f = await setup(workspace, wire, callback=callback)
    if phase == 'crash':
        def fault(where):
            if point == 'receipt_conflict' and where == 'draft':
                message = f.sessions._load('desktop:markdown').messages[-1]
                f.sessions._store.update_message(message['id'], delivery_status='sent', external_message_id='123')
                with f.jobs.db:
                    f.jobs.db.execute("UPDATE tasks SET state='blocked',error='source_history_changed'")
                os._exit(73)
            task = memory_task.get()
            is_callback = task is not None and task.row['id'].startswith('markdown-callback:')
            if callback and 'memory_commit' in where:
                is_callback = True  # Store transaction temporarily clears the context.
            # The model wrapper temporarily clears memory_task during transport.
            if where == 'model_response' and callback:
                is_callback = len(counter.read_text().splitlines()) == 3
            second_response = point == 'second_model_response' and where == 'model_response' and len(counter.read_text().splitlines()) == 2
            if where == point or second_response or (callback and is_callback and where == point.removeprefix('callback_')):
                os._exit(73)
        durable.fault = fault
        durable_tasks.fault = fault
        if point == 'source_fact':
            def committed(self):
                row = self.source.execute('SELECT payload FROM events ORDER BY seq DESC LIMIT 1').fetchone()
                if 'markdown_maintenance' in json.loads(row[0]).get('data', {}):
                    os._exit(73)
            ConversationProjection._after_fact_commit = committed
        await admit(f)
        await f.jobs.drain()
        if f.callback_jobs:
            await f.callback_jobs.drain()
        raise AssertionError('crash boundary not reached')
    try:
        await f.jobs.drain()
        if f.callback_jobs:
            await f.callback_jobs.drain()
        before = len(counter.read_text().splitlines()) if counter.exists() else 0
        await f.jobs.drain()
        if f.callback_jobs:
            await f.callback_jobs.drain()
        calls = len(counter.read_text().splitlines()) if counter.exists() else 0
        assert before == calls
        print(json.dumps({'calls':calls,'tasks':f.jobs.list_tasks(),
            'history':f.runtime.store.read_history().count('durable history'),
            'pending':f.runtime.store.read_pending().count('durable preference'),
            'recent':f.runtime.store.read_recent_context().count('durable topic'),
            'journal':sum(p.read_text(encoding='utf-8').count('durable history') for p in f.runtime.store.journal_dir.glob('*.md')),
            'cursor':f.sessions._load('desktop:markdown').last_consolidated,
            'callback_tasks':f.callback_jobs.list_tasks()['tasks'] if f.callback_jobs else [],
            'items':f.engine._v2_store.list_items_for_admin()[0] if f.callback_jobs else []}))
    finally:
        await close(f)

asyncio.run(main())
