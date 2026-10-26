"""Operator recovery must not turn uncertainty into an automatic duplicate send."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from bus.chat_lane import ChatLane
from bus.durable import TransportStore, Outbox, DeliveryStopped
from core.execution.journal import ExecutionJournal
from core.execution.scope import ExecutionScope, current_execution
from bus.queue import MessageBus


@pytest.fixture
def box(tmp_path):
    store = TransportStore(tmp_path)
    box = Outbox(store, ChatLane())
    yield box
    store.close()


def decide(box, key, *, decision='retry', absent=None, action='action', journal=None, revision=None):
    return box.store.review_job(key, expected_revision=box.store.get(key)['revision'] if revision is None else revision,
        action_id=action, decision=decision, note='Checked original account and destination',
        absent_parts=absent, journal=journal, accounts=box.accounts, handlers=box.handlers)


@pytest.mark.asyncio
async def test_partial_delivery_review_retry_keeps_receipts_and_audit(box):
    calls = []
    fail = True
    async def handler(payload):
        async def send(index):
            calls.append(index)
            if index == 2 and fail:
                raise TimeoutError('receipt unknown')
            return SimpleNamespace(message_id=index)
        for index in (1, 2):
            await box.part(str(index), {'text': str(index)}, lambda: send(index))
        return {'ok': True}
    box.handlers['bus:test'] = handler
    key = box.enqueue('bus:test', {'channel': 'test', 'chat_id': '1'}, 'send')
    with pytest.raises(TimeoutError):
        await box.deliver(key)
    version = box.store.get(key)['revision']
    with pytest.raises(ValueError, match='逐项'):
        decide(box, key)
    with pytest.raises(ValueError, match='逐项'):
        decide(box, key, absent=['1', '2'])
    reviewed = decide(box, key, absent=['2'], revision=version)
    assert reviewed['state'] == 'pending'
    assert reviewed['reviews'][0]['evidence']['parts'][1]['state'] == 'sending'
    assert reviewed['parts'][0]['result'] == {'message_id': '1'}
    assert len(decide(box, key, absent=['2'], revision=version)['reviews']) == 1
    with pytest.raises(ValueError, match='标识'):
        decide(box, key, decision='close', revision=version)
    fail = False
    assert await box.deliver(key) == {'ok': True}
    assert calls == [1, 2, 2]
    # A retried RPC after delivery finished must not requeue or re-send.
    assert decide(box, key, absent=['2'], revision=version)['state'] == 'done'
    assert await box.deliver(key) == {'ok': True}
    assert calls == [1, 2, 2]
    with pytest.raises(sqlite3.IntegrityError):
        with box.store.db:
            box.store.db.execute('DELETE FROM transport_reviews')


@pytest.mark.asyncio
async def test_close_preserves_unknown_state_and_blocks_worker_and_direct_send(box, tmp_path):
    box.handlers['test'] = lambda payload: None
    key = box.enqueue('test', {'channel': 'test', 'chat_id': '1'})
    box.store.state(key, 'needs_review', error='original uncertainty')
    reviewed = decide(box, key, decision='close')
    assert reviewed['state'] == 'needs_review' and reviewed['error'] == 'original uncertainty'
    with pytest.raises(DeliveryStopped, match='closed'):
        await box.deliver(key)
    box.store.close()
    restored = TransportStore(tmp_path)
    try:
        assert restored.get(key)['review_closed'] == 1
        assert restored.pending() == []
        with pytest.raises(ValueError, match='已结束'):
            restored.review_job(key, expected_revision=restored.get(key)['revision'], action_id='new',
                decision='retry', note='cannot reopen', accounts={}, handlers={'test': object()})
    finally:
        restored.close()


def test_stale_revision_part_changes_active_and_account_mismatch(box):
    box.handlers['test'] = object()
    box.accounts['telegram'] = 'old'
    key = box.enqueue('test', {'channel': 'telegram', 'chat_id': '1'})
    box.store.state(key, 'failed')
    version = box.store.get(key)['revision']
    with box.store.db:
        box.store.db.execute("INSERT INTO parts VALUES (?,?,'{}','pending',NULL)", (key, '1'))
    with pytest.raises(ValueError, match='已变化'):
        decide(box, key, revision=version)
    box.store.active.add(key)
    with pytest.raises(ValueError, match='处理中'):
        decide(box, key)
    box.store.active.clear()
    box.accounts['telegram'] = 'new'
    with pytest.raises(ValueError, match='账户'):
        decide(box, key)
    assert not box.store.db.execute('SELECT 1 FROM transport_reviews').fetchone()


def test_inbound_links_execution_and_refuses_failed_execution_replay(box, tmp_path):
    journal = ExecutionJournal(tmp_path)
    try:
        payload = {'channel': 'telegram', 'chat_id': '7', 'metadata': {'execution_request_id': 'input'}}
        box.store.put('in:bot:7:1', 'inbound', payload)
        box.store.state('in:bot:7:1', 'failed')
        row, _ = journal.admit('telegram:7', 'telegram', 'input', {})
        journal.start(row['turn_id'])
        journal.finish(row['turn_id'], 'failed')
        reply = box.enqueue('bus:telegram', {'channel': 'telegram', 'chat_id': '7'}, 'reply:in:bot:7:1')
        assert box.store.detail(reply, journal=journal)['executions'][0]['turn_id'] == row['turn_id']
        assert len(box.store.list_jobs(journal=journal, turn_id=row['turn_id'], status='')['jobs']) == 2
        with pytest.raises(ValueError, match='不能重跑模型'):
            decide(box, 'in:bot:7:1', journal=journal)
        # Closing an execution is independent of closing a queue job.
        journal.review(row['turn_id'], expected_seq=journal.get(row['turn_id'])['last_seq'],
                       action_id='exec-close', decision='close', note='checked')
        assert not box.store.get('in:bot:7:1')['review_closed']
        decide(box, 'in:bot:7:1', decision='close', journal=journal)
        assert box.store.get(reply)['state'] == 'pending'
    finally:
        journal.close()


def test_unadmitted_inbound_can_requeue_but_raw_commands_cannot(box, tmp_path):
    journal = ExecutionJournal(tmp_path)
    try:
        box.store.put('input', 'inbound', {'channel': 'telegram', 'chat_id': '1', 'metadata': {'execution_request_id': 'stable'}})
        box.store.state('input', 'failed')
        assert decide(box, 'input', journal=journal)['state'] == 'pending'
        box.store.put('raw', 'telegram-update:bot', {'message': {'text': '/stop'}})
        box.store.state('raw', 'failed')
        with pytest.raises(ValueError, match='原始更新'):
            decide(box, 'raw', action='raw', journal=journal)
    finally:
        journal.close()


def test_enqueue_captures_exact_execution_and_legacy_rows_remain_readable(box, tmp_path):
    journal = ExecutionJournal(tmp_path)
    try:
        row, _ = journal.admit('desktop:source', 'desktop', 'req', {})
        journal.start(row['turn_id'])
        token = current_execution.set(ExecutionScope(journal, row['turn_id']))
        try:
            key = box.enqueue('test', {'channel': 'test', 'chat_id': '1'})
        finally:
            current_execution.reset(token)
        assert box.store.detail(key, journal=journal)['executions'][0]['turn_id'] == row['turn_id']
        for i in range(7):
            box.store.put(f'legacy-{i}', 'test', {})
        first = box.store.list_jobs(status='', limit=3)
        second = box.store.list_jobs(status='', limit=3, before=first['next_cursor'])
        assert not {r['id'] for r in first['jobs']} & {r['id'] for r in second['jobs']}
        assert len(box.store.list_jobs(status='', journal=journal, session_key='desktop:source')['jobs']) == 1
    finally:
        journal.close()


@pytest.mark.parametrize('decision', ['close', 'retry'])
@pytest.mark.parametrize('boundary', ['before_commit', 'after_commit'])
def test_review_hard_crash_is_atomic_and_repeat_recovery_is_stable(tmp_path, decision, boundary):
    helper = Path(__file__).parents[1] / 'e2e' / 'transport_review_crash.py'
    result = subprocess.run([sys.executable, str(helper), str(tmp_path), decision, boundary],
                            env={**os.environ, 'PYTHONPATH': str(Path.cwd() / 'apps/backend')}, timeout=20)
    assert result.returncode == 73
    snapshots = []
    for _ in range(2):
        store = TransportStore(tmp_path)
        try:
            detail = store.detail('job', handlers={'test': object()}, accounts={})
            snapshots.append(json.dumps(detail, sort_keys=True))
            committed = boundary == 'after_commit'
            assert len(detail['reviews']) == int(committed)
            assert bool(detail['review_closed']) == (committed and decision == 'close')
            assert detail['state'] == ('pending' if committed and decision == 'retry' else 'needs_review')
            assert detail['parts'][0]['result']['message_id'] == 'one'
            assert detail['parts'][1]['state'] == ('pending' if committed and decision == 'retry' else 'sending')
        finally:
            store.close()
    assert snapshots[0] == snapshots[1]


def test_upgrade_existing_queue_keeps_original_payloads_and_receipts(tmp_path):
    directory = tmp_path / 'private_runtime' / 'transport'
    directory.mkdir(parents=True)
    db = sqlite3.connect(directory / 'queue.sqlite3')
    db.executescript("""CREATE TABLE jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
        due REAL NOT NULL DEFAULT 0,result TEXT,error TEXT);
        INSERT INTO jobs(id,kind,payload,state,result) VALUES('old','test','{}','done','{"message_id":"5"}');""")
    db.close()
    store = TransportStore(tmp_path)
    try:
        assert store.get('old')['result'] == {'message_id': '5'}
        assert store.detail('old')['reviews'] == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_old_inbox_account_cannot_run_on_new_bot(tmp_path):
    journal = ExecutionJournal(tmp_path)
    bus = MessageBus()
    bus.enable_durability(tmp_path, journal)
    try:
        bus.outbox.accounts['telegram'] = 'new-bot'
        key = 'in:telegram:old-bot:7:1'
        bus.transport.put(key, 'inbound', {'channel': 'telegram', 'chat_id': '7', 'metadata': {'execution_request_id': key}})
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(bus.consume_inbound(), .15)
        assert bus.transport.get(key)['state'] == 'needs_review'
        assert bus.transport.get(key)['attempts'] == 0
        with pytest.raises(ValueError, match='收件账户'):
            decide(bus.outbox, key, journal=journal)
        bus.outbox.accounts['telegram'] = 'old-bot'
        assert decide(bus.outbox, key, journal=journal)['state'] == 'pending'
    finally:
        bus.transport.close()
        journal.close()


def test_real_telegram_identity_links_raw_inbox_and_reply(box, tmp_path):
    journal = ExecutionJournal(tmp_path)
    try:
        raw, incoming = 'update:telegram:bot:5', 'in:telegram:bot:7:8'
        box.store.put(raw, 'telegram-update:telegram:bot', {'message': {'chat': {'id': 7}, 'message_id': 8}})
        box.store.put(incoming, 'inbound', {'channel': 'telegram', 'chat_id': '7', 'metadata': {'execution_request_id': incoming}})
        reply = box.enqueue('bus:telegram', {'channel': 'telegram', 'chat_id': '7'}, 'reply:' + incoming)
        row, _ = journal.admit('telegram:7', 'telegram', incoming, {})
        assert box.store.detail(reply, journal=journal)['related_jobs'] == sorted([raw, incoming, reply])
        assert box.store.detail(raw, journal=journal)['executions'][0]['turn_id'] == row['turn_id']
    finally:
        journal.close()
