"""Real authenticated remote Host shares the local operator queue API."""
import asyncio

import pytest

from tests.backend.bootstrap.test_remote_host import remote, rpc, authenticate  # noqa: F401


@pytest.mark.asyncio
async def test_remote_queue_close_and_lost_response_are_idempotent(remote):
    store = remote.server.service.runtime.bus.transport
    store.put('remote-job', 'inbound', {'channel': 'telegram', 'chat_id': '7', 'metadata': {'execution_request_id': 'r'}})
    store.state('remote-job', 'failed')
    first = await authenticate(remote.url)
    second = await authenticate(remote.url)
    try:
        listed = await rpc(first, 'transport.list')
        assert listed['payload']['jobs'][0]['id'] == 'remote-job'
        detail = (await rpc(first, 'transport.get', {'job_id': 'remote-job'}))['payload']
        payload = dict(job_id='remote-job', expected_revision=detail['revision'], action_id='close',
                       decision='close', note='verified original sender')
        assert not (await rpc(first, 'transport.review', payload)).get('error')
        await first.close()
        again = (await rpc(second, 'transport.review', payload))['payload']
        assert len(again['reviews']) == 1 and again['review_closed'] == 1
        assert again['state'] == 'failed'
        assert not remote.calls
        assert not (await rpc(second, 'transport.list'))['payload']['jobs']
        assert (await rpc(second, 'transport.list', {'status': 'closed'}))['payload']['jobs']
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_remote_racing_retry_and_close_have_one_winner(remote):
    store = remote.server.service.runtime.bus.transport
    # No channel/worker owns this fixture. A retry would be admitted once only.
    store.put('race', 'inbound', {'channel': 'telegram', 'chat_id': '7', 'metadata': {'execution_request_id': 'race'}})
    store.state('race', 'failed')
    revision = store.get('race')['revision']
    first, second = await authenticate(remote.url), await authenticate(remote.url)
    try:
        results = await asyncio.gather(*[
            rpc(socket, 'transport.review', dict(job_id='race', expected_revision=revision,
                action_id=decision, decision=decision, note='verified'))
            for socket, decision in [(first, 'retry'), (second, 'close')]])
        assert sum(not result.get('error') for result in results) == 1
        assert store.db.execute('SELECT count(*) FROM transport_reviews').fetchone()[0] == 1
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_execution_clearance_requires_queue_closed_and_no_successful_part(remote):
    runtime = remote.server.service.runtime
    journal, store = runtime.session_manager.execution_journal, runtime.bus.transport
    row, _ = journal.admit('desktop:linked', 'desktop', 'linked', {})
    journal.start(row['turn_id'])
    journal.append(row['turn_id'], 'delivery.dispatched', {'effect_id': 'send'})
    journal.finish(row['turn_id'], 'failed')
    store.put('linked-send', 'fixture', {})
    store.link_execution('linked-send', row['turn_id'])
    store.state('linked-send', 'needs_review')
    payload = dict(turn_id=row['turn_id'], expected_seq=journal.get(row['turn_id'])['last_seq'],
                   action_id='clearance', decision='no_effect', note='checked')
    socket = await authenticate(remote.url)
    try:
        blocked = await rpc(socket, 'execution.review', payload)
        assert blocked['error'] and '关联发件队列' in blocked['error']['message']
        close = dict(job_id='linked-send', expected_revision=store.get('linked-send')['revision'],
                     action_id='queue-close', decision='close', note='stop future send')
        assert not (await rpc(socket, 'transport.review', close)).get('error')
        with store.db:
            store.db.execute("INSERT INTO parts VALUES ('linked-send','1','{}','done','{}')")
        assert (await rpc(socket, 'execution.review', payload))['error']
        assert journal.get(row['turn_id'])['review'] is None
        assert not remote.calls
    finally:
        await socket.close()
