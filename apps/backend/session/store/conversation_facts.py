"""Versioned conversation facts and a pure, effect-free projection adapter.

Execution facts remain available for future model/recovery projections. These
facts describe accepted conversation changes, including explicit edits/deletes.
"""
from __future__ import annotations

import json

VERSION = 1
TABLES = {"sessions": "key", "messages": "id"}
JSON_FIELDS = {'metadata', 'tool_chain', 'extra', 'media'}
MESSAGE_FIELDS = {'id', 'session_key', 'seq', 'role', 'content', 'tool_chain', 'extra',
                  'ts', 'thread_id', 'sender_role', 'media', 'external_message_id', 'delivery_status'}
SESSION_FIELDS = {'key', 'created_at', 'updated_at', 'last_consolidated', 'metadata',
                  'last_user_at', 'last_proactive_at', 'next_seq'}


def conversation_fact(entity: str, identity: str, row: dict | None) -> dict:
    noun = 'session' if entity == 'sessions' else 'message'
    if row is None:
        return {'kind': noun + '.deleted', 'id': identity}
    expected = SESSION_FIELDS if entity == 'sessions' else MESSAGE_FIELDS
    if set(row) != expected or row[TABLES[entity]] != identity:
        raise ValueError('Unsupported conversation schema or identity')
    data = {k: json.loads(v) if k in JSON_FIELDS and v is not None else v for k, v in row.items()}
    if entity == 'messages':
        data['timestamp'] = data.pop('ts')
        data['attributes'] = data.pop('extra')
    return {'kind': noun + '.recorded', 'id': identity, 'data': data}


def project_conversation_facts(facts: list[dict]) -> list[tuple[str, str, dict | None]]:
    """Same fact prefix gives the same rows; no storage, clock or random access."""
    result = []
    identities = set()
    for fact in facts:
        kind = fact['kind']
        if kind not in {'message.recorded', 'message.deleted', 'session.recorded', 'session.deleted'}:
            raise ValueError('Unsupported conversation fact kind')
        table = 'messages' if kind.startswith('message.') else 'sessions'
        identity = fact['id']
        if not isinstance(identity, str) or not identity or (table, identity) in identities:
            raise ValueError('Invalid or repeated conversation identity')
        identities.add((table, identity))
        row = None
        if kind.endswith('.recorded'):
            data = dict(fact['data'])
            if table == 'messages':
                data['ts'] = data.pop('timestamp')
                data['extra'] = data.pop('attributes')
            row = {k: json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS and v is not None else v for k, v in data.items()}
            # Validate shape before any consumer receives a partial projection.
            conversation_fact(table, identity, row)
        result.append((table, identity, row))
    return result
