"""Local and authenticated remote RPCs share this Host-owned continuation seam."""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import asdict
from datetime import datetime
from os import getcwd
from pathlib import Path

from bus.events import OutboundMessage
from core.execution.continuation import REASONS, claim_continuation, plan_continuation, resolve_boundary
from core.execution.projection import ExecutionProjectionError
from core.execution.contracts import ExecutionUnavailable, JournalWriteError
from core.execution.model_context import digest
from core.execution.scope import ExecutionScope, current_execution
from agent.lifecycle.types import PromptRenderInput


def inspect_host(service, turn_id):
    runtime, journal = service.runtime, service.sessions.execution_journal
    source = resolve_boundary(journal, turn_id)[0]
    blockers = []
    # Conservative v1: no overlapping execution or unsettled spawned work.
    if journal._conn.execute("SELECT 1 FROM executions WHERE status IN ('accepted','running') LIMIT 1").fetchone():
        blockers.append('background_unsettled')
    spawn = runtime.tools.get_tool('spawn')
    if spawn is not None and (not hasattr(spawn, 'manager') or spawn.manager.get_running_count()):
        blockers.append('background_unsettled')
    if service._requests or service._pending_chat_writes or runtime.loop._active_tasks:
        blockers.append('background_unsettled')
    snapshot = service.sessions._store.committed_history_snapshot(source['session_key'])
    from core.execution.continuation import read_events
    visible_ids = {m['id'] for m in snapshot['messages']}
    ancestor = source
    while True:
        for event in read_events(journal, ancestor['turn_id']):
            if event['kind'] == 'messages.committed' and any(
                    m.get('role') == 'user' and m.get('id') and m['id'] not in visible_ids
                    for m in event['payload'].get('messages', [])):
                blockers.append('session_changed')
        if not ancestor.get('continuation'):
            break
        ancestor = resolve_boundary(journal, ancestor['continuation']['source_turn_id'])[0]
    if service.sessions._store.get_session_meta(source['session_key']) is None or journal._conn.execute(
            "SELECT 1 FROM executions WHERE session_key=? AND rowid>? AND source IN ('desktop','telegram','proactive') LIMIT 1",
            (source['session_key'], source['cursor'])).fetchone():
        blockers.append('session_changed')
    queue = runtime.bus.transport
    queues = []
    if queue is None:
        blockers.append('queue_unsettled')
    else:
        for row in queue.db.execute('SELECT j.id,j.state,j.revision FROM jobs j JOIN transport_execution_links l ON j.id=l.job WHERE l.turn_id=? ORDER BY j.id', (turn_id,)):
            queues.append(dict(row))
            if row['state'] != 'done' or row['id'] in queue.active:
                blockers.append('queue_unsettled')
    return {'workspace': str(service.workspace.resolve()), 'cwd': str(Path(getcwd()).resolve()),
            'tools': sorted(runtime.tools.get_registered_names()),
            'catalog_digest': digest(runtime.tools.get_schemas()),
            'session_digest': digest({'messages': snapshot['messages'], 'links': snapshot['links']}),
            'queues': queues, 'blockers': sorted(set(blockers))}


def recovery_plan(service, turn_id):
    journal = service.sessions.execution_journal
    source = journal._conn.execute('SELECT source FROM executions WHERE turn_id=?', (turn_id,)).fetchone()
    if source is not None and source['source'] == 'proactive':
        from .proactive_recovery_service import recovery_plan as proactive_plan
        return proactive_plan(service, turn_id)
    with journal._lock:
        try:
            observation = inspect_host(service, turn_id)
        except (ValueError, KeyError, TypeError, ExecutionProjectionError):
            return {'version': 1, 'turn_id': turn_id, 'allowed': False, 'plan_digest': '',
                    'reasons': ['history_invalid'], 'explanations': [REASONS['history_invalid']]}
        except (AttributeError, sqlite3.DatabaseError):
            return {'version': 1, 'turn_id': turn_id, 'allowed': False, 'plan_digest': '',
                    'reasons': ['safety_observation_unavailable'], 'explanations': [REASONS['safety_observation_unavailable']]}
        return plan_continuation(journal, turn_id, observation)


async def continue_execution(service, payload, request_id, chat_admission):
    journal = service.sessions.execution_journal
    turn_id = str(payload.get('turn_id') or '')
    if journal.get(turn_id)['source'] == 'proactive':
        from .proactive_recovery_service import continue_execution as proactive_continue
        return await proactive_continue(service, payload, request_id, chat_admission)
    if payload.get('confirmed') is not True:
        raise ValueError('请先查看恢复计划，再明确确认创建续跑')
    # Acquire capacity BEFORE revalidation. No await between observation, claim,
    # and registration in the Host's active request map.
    async with chat_admission():
        record, created = claim_continuation(journal, turn_id, payload.get('plan_digest'), payload.get('action_id'),
                                             lambda: inspect_host(service, turn_id))
        if not created:
            return {'execution': journal.get(record['turn_id']), 'created': False}
        key = record['session_key']
        service._requests[key] = request_id
        service._chat_tasks[key] = asyncio.current_task()
        scope = ExecutionScope(journal, record['turn_id'])
        token = current_execution.set(scope)
        try:
            journal.start(scope.turn_id)
            _, _, _, history, effects = resolve_boundary(journal, turn_id)
            scope.completed_ancestor_tools = {
                digest({'name': e['name'], 'arguments': e['model_arguments']})
                for e in effects if e['kind'] == 'tool' and 'model_arguments' in e}
            # Current system/skills/tools are assembled anew. Historical requests
            # remain exactly reconstructable, but are never retransmitted here.
            session = service.sessions.get_or_create(key)
            root_source = journal.get(turn_id)
            while root_source.get('continuation'):
                root_source = journal.get(root_source['continuation']['source_turn_id'])
            snapshot = service.sessions._store.committed_history_snapshot(key)
            if not any(m['role'] == 'user' and snapshot['links'].get(m['id'], {}).get('turn_id') == root_source['turn_id']
                       for m in snapshot['messages']):
                # This is a presentation of the existing accepted input, not a
                # second user interaction. Stable write ID survives view failure.
                input_token = current_execution.set(None)
                try:
                    session.add_message('user', root_source['input']['content'], media=root_source['input'].get('media'),
                        session_write_id='continuation-input:' + root_source['turn_id'],
                        metadata={'execution_request_id': root_source['request_key'], 'source': root_source['source']})
                    await service.sessions.append_messages(session, [session.messages[-1]])
                finally:
                    current_execution.reset(input_token)
            reasoner = service.runtime.loop._reasoner
            rendered = await reasoner.render_prompt(PromptRenderInput(
                session_key=key, channel='desktop', chat_id=key, content='', history=[],
                media=None, timestamp=datetime.now(), skill_names=None, retrieved_memory_block='',
                disabled_sections=set(), turn_injection_prompt='',
                session_metadata=dict(session.metadata)))
            messages = [m for m in rendered.messages if m.get('role') in {'system', 'developer'}] + history
            source_metadata = root_source['input'].get('metadata') or {}
            disabled = source_metadata.get('disabled_tools') or []
            disabled = [disabled] if isinstance(disabled, str) else disabled
            current_snapshot = service.sessions._store.committed_history_snapshot(key)
            source_ref = next((m['id'] for m in current_snapshot['messages'] if m['role'] == 'user' and
                              current_snapshot['links'].get(m['id'], {}).get('turn_id') == root_source['turn_id']), '')
            async with service.runtime.bus.chat_lane.passive_turn('desktop', key):
                result = await reasoner.run(messages, tool_event_session_key=key, tool_event_channel='desktop',
                    tool_event_chat_id=key, preloaded_tools=service.runtime.tools.get_registered_names(),
                    disabled_tools=set(disabled),
                    tool_execution_context={'channel': 'desktop', 'chat_id': key, 'session_key': key,
                        'current_user_message': root_source['input'].get('content', ''),
                        'thread_id': str(source_metadata.get('thread_id') or session.metadata.get('thread_id') or ''),
                        'delivery_key': '', 'session_config_version': '', 'current_timestamp': datetime.now().isoformat(),
                        'current_user_source_ref': source_ref, 'defer_push_session_sync': 'true'})
                if scope.broken or scope.review_required:
                    raise JournalWriteError('续跑记录未完整保存，请核对执行')
                # Persist only the new assistant presentation. The accepted input
                # remains owned by the source execution; no duplicate user turn.
                session.add_message('assistant', result.reply, tool_chain=result.metadata.get('tool_chain'),
                                    metadata={'continuation_of': turn_id})
                await service.sessions.append_messages(session, [session.messages[-1]])
                reply = OutboundMessage(channel='desktop', chat_id=key, content=result.reply)
                journal.finish(scope.turn_id, 'completed', asdict(reply))
            response = {'session_key': key, 'reply': result.reply, 'session': service._snapshot(key),
                        'execution': journal.get(scope.turn_id), 'created': True}
            await service.publish_event({'id': request_id, 'type': 'event', 'method': 'chat.done', 'payload': response})
            return response
        except BaseException as exc:
            if not scope.broken and journal.get(scope.turn_id)['status'] in {'accepted', 'running'}:
                journal.finish(scope.turn_id, 'interrupted' if isinstance(exc, asyncio.CancelledError) else 'failed')
            if isinstance(exc, asyncio.CancelledError):
                return {'execution': journal.get(scope.turn_id), 'created': True, 'cancelled': True,
                        'session_key': key, 'session': service._snapshot(key)}
            if isinstance(exc, Exception):
                raise ExecutionUnavailable('续跑已接纳但未完成；请查看关联执行，勿重复创建', journal.get(scope.turn_id)) from exc
            raise
        finally:
            scope.active = False
            current_execution.reset(token)
            service._requests.pop(key, None)
            service._chat_tasks.pop(key, None)
            service._chat_interrupts.pop(key, None)
