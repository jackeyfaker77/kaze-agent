"""Message evidence for background memory; task IDs are not message IDs."""
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import sqlite3
from contextlib import closing

from core.memory.task_context import MemoryTaskBlocked


def fingerprint(message):
    value = {k: message.get(k) for k in ('id', 'session_key', 'role', 'content')}
    value['timestamp'] = message.get('timestamp') or message.get('ts')
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def source_ref(messages, root):
    # The suffix survives recall/injection and binds the whole evidence group.
    versions = sorted((m['id'], m.get('revision') or fingerprint(m)) for m in messages)
    revision = hashlib.sha256(json.dumps([root, versions], ensure_ascii=False).encode()).hexdigest()
    return json.dumps([m['id'] for m in messages], ensure_ascii=False) + '#rev:' + revision


def _indexed_prefix(journal, ids):
    path = journal.directory.parent.parent / 'sessions.db'
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as view:
        view.execute('BEGIN')
        cursor = view.execute('SELECT root,cursor,digest FROM conversation_projection WHERE id=1').fetchone()
        if not cursor or cursor[0] != journal.root_id:
            return None
        boundary = journal._conn.execute("SELECT json_extract(payload,'$.digest') FROM events WHERE seq=? AND turn_id=?",
                                         (cursor[1], 'conversation:sessions.db')).fetchone()
        if not boundary or cursor[2] != boundary[0]:
            raise MemoryTaskBlocked('source_projection_mismatch')
        seqs = [r[0] for r in view.execute('SELECT DISTINCT event_seq FROM conversation_message_sources WHERE message_id IN (' +
                                           ','.join('?' for _ in ids) + ')', ids)]
        return cursor[1], seqs


def read_messages(journal, *, ids=None, turn=None, through=None):
    """Read committed facts without constructing/rebuilding a SessionStore."""
    from session.store.conversation_facts import project_conversation_facts
    from session.store.projection import encode
    where, args = '', []
    if ids is not None:
        if not ids:
            return []
        where = " AND json_extract(f.value,'$.id') IN (" + ','.join('?' for _ in ids) + ')'
        args.extend(ids)
        index = _indexed_prefix(journal, ids) if through is None else None
        if index:
            cursor, seqs = index
            # Exact indexed fact(s), plus the unprojected tail after a crash.
            restriction = 'e.seq>?' + (' OR e.seq IN (' + ','.join('?' for _ in seqs) + ')' if seqs else '')
            where += ' AND (' + restriction + ')'
            args.extend([cursor, *seqs])
    elif turn:
        where = " AND (json_extract(f.value,'$.interaction_turn_id')=? OR json_extract(f.value,'$.content_source.turn_id')=?)"
        args.extend((turn, turn))
        start = journal._conn.execute('SELECT MIN(seq) FROM events WHERE turn_id=?', (turn,)).fetchone()[0]
        if start is not None:
            where += ' AND e.seq>=?'
            args.append(start)
    else:
        return []
    if through is not None:
        where += ' AND e.seq<=?'
        args.append(through)
    rows = journal._conn.execute("""SELECT e.seq,e.payload,f.value FROM events e,
        json_each(e.payload,'$.data.facts') f
        WHERE e.kind IN ('conversation.baseline','conversation.changed')
        AND json_extract(f.value,'$.kind') IN ('message.recorded','message.deleted')""" + where + ' ORDER BY e.seq', args)
    latest = {}
    for row in rows:
        envelope, fact = json.loads(row[1]), json.loads(row[2])
        data = envelope['data']
        if data.get('root_id') != journal.root_id or hashlib.sha256(encode(data).encode()).hexdigest() != envelope.get('digest'):
            raise MemoryTaskBlocked('source_fact_corrupt')
        semantic = {}
        ref = fact.get('content_source')
        if ref:
            event = journal._conn.execute('SELECT * FROM events WHERE seq=?', (ref['seq'],)).fetchone()
            if event is None or ref['seq'] >= row[0]:
                raise MemoryTaskBlocked('source_identity_missing')
            semantic[event['seq']] = {**dict(event), 'payload': json.loads(event['payload'])}
        _, identity, value = project_conversation_facts([fact], semantic)[0]
        latest[identity] = value
    return sorted((m for m in latest.values() if m is not None), key=lambda m: (m['session_key'], m['seq']))


@dataclass
class SourceBatch:
    journal: object
    messages: list[dict]

    _validated_head: int | None = None

    @property
    def root(self):
        return self.journal.root_id

    def validate(self):
        if not self.messages or any(not m.get('id') for m in self.messages):
            raise MemoryTaskBlocked('source_identity_missing')
        head = self.journal._conn.execute("SELECT MAX(seq) FROM events WHERE turn_id='conversation:sessions.db'").fetchone()[0]
        if head is not None and self._validated_head == head:
            return
        current = read_messages(self.journal, ids=[m['id'] for m in self.messages])
        expected = {m['id']: m.get('revision') or fingerprint(m) for m in self.messages}
        if {m['id']: fingerprint(m) for m in current} != expected:
            raise MemoryTaskBlocked('source_history_changed')
        self._validated_head = head


memory_sources: ContextVar[SourceBatch | None] = ContextVar('memory_sources', default=None)
