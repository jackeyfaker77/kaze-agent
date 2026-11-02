"""Cross-route behavior against real conversation facts and persistent receipts."""
import json
from types import SimpleNamespace

import pytest

from agent.tools.message_lookup import FetchMessagesTool
from core.memory.events import ConsolidationCommitted
from core.memory.sources import read_messages
from memory2.memorizer import Memorizer
from plugins.default_memory.engine.lifecycle import DefaultMemoryEngine
from tests.backend.memory2.test_durable_tasks import Embedder, admit, close, setup


async def fixture(tmp_path):
    f = await setup(tmp_path)
    turn = await admit(f)
    f.jobs.discover()
    f.message = next(m for m in read_messages(f.journal, turn=turn) if m['role'] == 'user')
    f.engine = DefaultMemoryEngine.__new__(DefaultMemoryEngine)
    f.engine._v2_store = f.store
    f.engine.memory_tasks = f.jobs
    f.engine._memorizer = Memorizer(f.store, Embedder())
    f.jobs.consolidation_handler = f.engine._apply_consolidation
    f.prompts = []

    async def extract(**kwargs):
        f.prompts.append(kwargs['conversation'])
        return {'preference': [{'summary': 'prefers Chinese', 'source_id': f.message['id']}]}

    async def post(**kwargs):
        await f.engine._extract_and_save_post_response(user_msg=kwargs['user_msg'],
            assistant_response=kwargs['agent_response'], source_ref=kwargs['source_ref'],
            channel=kwargs['channel'], chat_id=kwargs['chat_id'], role_id='')

    f.engine._extract_implicit_long_term = extract
    f.jobs.worker = SimpleNamespace(run=post)
    return f


def archive(f, messages, key='archive', seq=999):
    return f.jobs.enqueue_consolidation(ConsolidationCommitted([], 'markdown-task:'+key, 'desktop', 'memory',
        '', key, 'desktop:memory', seq, None, messages), '')


@pytest.mark.parametrize('archive_first', [False, True])
async def test_two_routes_one_extraction_original_fetch_and_undo(tmp_path, archive_first):
    f = await fixture(tmp_path)
    try:
        archive(f, [f.message], seq=0 if archive_first else 999)
        await f.jobs.drain()
        assert len(f.prompts) == 1
        assert all(t['state'] == 'done' for t in f.jobs.list_tasks()['tasks'])
        assert f.store._db.execute('SELECT state FROM memory_coverage').fetchall() == [('written',)]
        item = f.store.get_items_by_ids([f.store.list_by_type('preference')[0]['id']])[0]
        reply = json.loads(await FetchMessagesTool(f.sessions._store).execute(source_ref=item['source_ref']))
        assert [m['content'] for m in reply['messages']] == [f.message['content']]
        assert f.engine.undo_by_message_sources([f.message['id']])['affected_ids'] == [item['id']]
        archive(f, [f.message], key='late')
        await f.jobs.drain()
        assert len(f.prompts) == 1
        assert f.store.get_items_by_ids([item['id']])[0]['status'] == 'superseded'
    finally:
        await close(f)


@pytest.mark.parametrize('mode', ['empty', 'budget', 'unknown', 'edited'])
async def test_coverage_outcomes_do_not_silently_mark_a_failed_source_done(tmp_path, mode):
    f = await fixture(tmp_path)
    try:
        original = f.engine._extract_implicit_long_term
        async def extract(**kwargs):
            f.prompts.append(kwargs['conversation'])
            if mode == 'unknown':
                raise RuntimeError('no response')
            if mode == 'edited':
                f.sessions._store.update_message(f.message['id'], content='edited original')
                return {'preference':[{'summary':'stale extraction'}]}
            return {'preference':[]}
        if mode == 'budget':
            from memory2.post_response_worker import PostResponseMemoryWorker
            worker = PostResponseMemoryWorker(f.engine._memorizer, None, f.provider, 'test',
                                               implicit_memory_handler=f.engine._extract_and_save_post_response)
            worker.TOKEN_BUDGET_PER_RUN = 0
            f.jobs.worker = worker
        else:
            f.engine._extract_implicit_long_term = extract
        await f.jobs.drain()
        state = f.store._db.execute('SELECT state FROM memory_coverage').fetchone()[0]
        assert state == {'budget':'deferred','empty':'empty','unknown':'blocked','edited':'blocked'}[mode]
        f.engine._extract_implicit_long_term = original
        archive(f, [f.message])
        await f.jobs.drain()
        assert len(f.prompts) == 1
        assert len(f.store.list_by_type('preference')) == int(mode == 'budget')
    finally:
        await close(f)


@pytest.mark.parametrize('point', ['before_memory_commit:finish_extraction','after_memory_commit:finish_extraction'])
async def test_restart_commits_extraction_and_coverage_together(tmp_path, monkeypatch, point):
    from memory2 import durable_tasks
    class Crash(BaseException):
        pass
    f = await fixture(tmp_path)
    try:
        def fault(name):
            if name == point:
                raise Crash()
        monkeypatch.setattr(durable_tasks, 'fault', fault)
        with pytest.raises(Crash):
            await f.jobs.drain()
        assert f.store._db.execute('SELECT state FROM memory_coverage').fetchone()[0] == (
            'written' if point.startswith('after') else 'processing')
        assert len(f.store.list_by_type('preference')) == int(point.startswith('after'))
        # Simulate takeover from a dead owner; real process/lease boundaries are
        # exercised by test_durable_tasks_process with os._exit.
        with f.store._db:
            f.store._db.execute("UPDATE memory_tasks SET owner='dead-epoch' WHERE state='running'")
        monkeypatch.setattr(durable_tasks, 'fault', lambda _: None)
        await f.jobs.drain()
        assert f.jobs.list_tasks()['tasks'][0]['state'] == 'done'
        assert f.store._db.execute('SELECT state FROM memory_coverage').fetchall() == [('written',)]
        assert f.store.list_by_type('preference')[0]['reinforcement'] == 1
    finally:
        await close(f)


async def test_independent_sources_survive_partial_undo(tmp_path):
    f = await fixture(tmp_path)
    try:
        key = f.message['id']
        def put(ref):
            return f.store.commit_change('add', memory_type='preference', summary='prefers Chinese',
                embedding=None, source_ref=ref, extra={}).split(':')[1]
        item = put(key)
        await f.jobs.drain()
        assert f.store.list_by_type('preference')[0]['reinforcement'] == 1
        assert put('telegram:other:2') == item
        assert f.engine.undo_by_message_sources([key])['affected_ids'] == []
        live = f.store.get_items_by_ids([item])[0]
        assert live['status'] == 'active' and live['source_ref'] == 'telegram:other:2'
        assert f.engine.undo_by_message_sources(['telegram:other:2'])['affected_ids'] == [item]
    finally:
        await close(f)


async def test_legacy_evidence_lookup_and_undo_preserve_new_confirmation(tmp_path):
    f = await fixture(tmp_path)
    try:
        job = f.jobs.list_tasks()['tasks'][0]['id']
        legacy = 'memory-task:' + job
        item = f.store.upsert_item('profile', 'shared fact', None, legacy).split(':')[1]
        f.store.commit_change('add', memory_type='profile', summary='shared fact', embedding=None,
                              source_ref='telegram:other:1')
        tool = FetchMessagesTool(f.sessions._store, f.jobs.legacy_ref)
        assert json.loads(await tool.execute(source_ref=legacy+'#implicit'))['matched_count'] == 1
        assert f.engine.undo_by_message_sources([f.message['id']])['affected_ids'] == []
        survivor = f.store.get_items_by_ids([item])[0]
        assert survivor['status'] == 'active' and survivor['source_ref'] == 'telegram:other:1'
    finally:
        await close(f)


async def test_old_unknown_cannot_be_retried_through_new_archive(tmp_path):
    f = await fixture(tmp_path)
    try:
        with f.store._db:
            f.store._db.execute("UPDATE memory_tasks SET version=2,state='blocked',error='model_outcome_unknown'")
        archive(f, [f.message])
        await f.jobs.drain()
        assert not f.prompts
        assert {t['error'] for t in f.jobs.list_tasks()['tasks']} == {'model_outcome_unknown','legacy_source_unresolved'}
    finally:
        await close(f)


async def test_versioned_evidence_ref_rejects_edits_but_direct_lookup_reads_current_message(tmp_path):
    f = await fixture(tmp_path)
    try:
        await f.jobs.drain()
        item = f.store.list_items_for_admin(memory_type='preference')[0][0]
        legacy = 'memory-task:' + f.jobs.list_tasks()['tasks'][0]['id']
        f.sessions._store.update_message(f.message['id'], content='new text')
        tool = FetchMessagesTool(f.sessions._store, f.jobs.legacy_ref)
        for ref in (item['source_ref'], legacy):
            for ctx in (0, 1):
                response = json.loads(await tool.execute(evidence=[{'source_ref':ref}], context=ctx))
                assert response['error'] == 'source_history_changed' and response['messages'] == []
        direct = json.loads(await tool.execute(ids=[f.message['id']]))
        assert direct['messages'][0]['content'] == 'new text'
    finally:
        await close(f)


async def test_undo_replacement_respects_separate_forget_until_both_are_withdrawn(tmp_path):
    f = await fixture(tmp_path)
    try:
        put = dict(memory_type='profile', embedding=None, extra={})
        old = f.store.commit_change('add', **put, summary='old', source_ref='a').split(':')[1]
        f.store.commit_change('replace', **put, summary='new', supersedes=[old], source_ref='b')
        f.store.commit_change('forget', ids=[old], source_ref='c')
        assert f.engine.undo_by_message_sources(['b'])['restored_ids'] == []
        assert f.store.get_items_by_ids([old])[0]['status'] == 'superseded'
        assert f.engine.undo_by_message_sources(['c'])['restored_ids'] == [old]
    finally:
        await close(f)


async def test_unresolved_old_turn_does_not_freeze_later_new_message(tmp_path):
    f = await fixture(tmp_path)
    try:
        with f.store._db:
            f.store._db.execute("INSERT INTO memory_tasks VALUES ('unresolved',?,0,'no-user',2,?,'blocked',NULL,'unknown')",
                                (f.journal.root_id, json.dumps({'session_key':'desktop:memory'})))
        await f.jobs.drain()
        assert len(f.prompts) == 1 and f.store.list_by_type('preference')
    finally:
        await close(f)


async def test_cannot_bind_a_different_journal_to_same_memory_database(tmp_path):
    from memory2.durable_tasks import MemoryTasks
    from core.memory.task_context import MemoryTaskBlocked
    f = await fixture(tmp_path)
    try:
        with pytest.raises(MemoryTaskBlocked, match='root_mismatch'):
            MemoryTasks(f.store, SimpleNamespace(root_id='other'), None)
        assert f.store._db.execute('SELECT root FROM memory_root').fetchone() == (f.journal.root_id,)
    finally:
        await close(f)


async def test_legacy_archive_evidence_excludes_tool_content_and_stays_bounded_in_prompt(tmp_path):
    import sqlite3
    from contextlib import closing
    from memory2.retriever import _format_source_tag
    f = await fixture(tmp_path)
    try:
        path = tmp_path / 'memory' / 'markdown_tasks.sqlite3'
        path.parent.mkdir(exist_ok=True)
        messages = [dict(f.message, id=f'user:{i}') for i in range(99)]
        messages.append(dict(f.message, role='tool', id='tool:private', content='private tool result'))
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('CREATE TABLE tasks(root,id,session_key,draft,snapshot)')
            conn.execute('INSERT INTO tasks VALUES (?,?,?,?,?)', (f.journal.root_id,'old-window',
                'desktop:memory',json.dumps({'window':{'old_messages':messages}}),'{}'))
            conn.commit()
        handle = 'markdown-task:old-window#h:entry'
        assert f.jobs.legacy_sources(handle) == [m['id'] for m in messages[:-1]]
        ref = f.jobs.legacy_ref(handle)
        assert 'tool:private' not in ref and len(_format_source_tag(ref)) < 80
    finally:
        await close(f)


async def test_unresolved_source_does_not_block_unrelated_extraction(tmp_path):
    from core.memory.sources import fingerprint
    f = await fixture(tmp_path)
    try:
        session = f.sessions.get_or_create('desktop:memory')
        session.add_message('user', 'another message')
        await f.sessions.save_async(session)
        second, = read_messages(f.journal, ids=[f.sessions._load(session.key).messages[-1]['id']])
        with f.store._db:
            f.store._db.execute("INSERT INTO memory_coverage VALUES (?,?,?,'blocked','old','unknown','')",
                                (f.journal.root_id, second['id'], fingerprint(second)))
        archive(f, [f.message, second], seq=0)
        await f.jobs.drain()
        assert len(f.prompts) == 1 and second['content'] not in f.prompts[0]
        assert dict(f.store._db.execute('SELECT message_id,state FROM memory_coverage')) == {
            f.message['id']:'written', second['id']:'blocked'}
    finally:
        await close(f)
