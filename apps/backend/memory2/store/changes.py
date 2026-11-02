"""Common transaction, evidence and replay rules for memory changes."""
from contextlib import contextmanager
import hashlib
import json

from core.memory.sources import fingerprint, memory_sources, source_ref as message_ref
from core.memory.task_context import MemoryTaskBlocked
from .common import _content_hash, _now_iso


def evidence_ids(ref):
    if not ref:
        return []
    head = ref.split('#', 1)[0]
    if head.startswith('['):
        try:
            ids = json.loads(head)
        except ValueError:
            return []
        return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []
    return [head]


def same_evidence(first, second):
    return bool(first and second and set(evidence_ids(first)) == set(evidence_ids(second)))


def owner(extra):
    return (str(extra.get('role_id') or '').strip(), str(extra.get('memory_domain') or 'shared').strip())


def compatible(item, extra):
    old = item.get('extra_json') or {}
    return owner(old) == owner(extra) and (owner(old)[1] == 'shared' or all(
        not old.get(k) or old.get(k) == extra.get(k) for k in ('scope_channel', 'scope_chat_id')))


class _StoreChangesMixin:
    @contextmanager
    def memory_transaction(self):
        with self._lock:
            db = self._db
            outer = db.defer_commit
            db.execute('SAVEPOINT memory_change')
            db.defer_commit = True
            try:
                yield
                db.execute('RELEASE memory_change')
            except BaseException:
                db.execute('ROLLBACK TO memory_change')
                db.execute('RELEASE memory_change')
                raise
            finally:
                db.defer_commit = outer

    def commit_change(self, action, *, source_ref='', extra=None, **payload):
        """Route add/replace/retire through one atomic, source-aware boundary."""
        extra = extra or {}
        batch = memory_sources.get()
        if batch:
            batch.validate()
        identity = json.dumps([batch.root if batch else '', action, source_ref, extra,
                               {k: v for k, v in payload.items() if k != 'embedding'}], sort_keys=True, ensure_ascii=False)
        if batch:
            identity += json.dumps([(m['id'], m.get('revision') or fingerprint(m)) for m in batch.messages])
        key = hashlib.sha256(identity.encode()).hexdigest()
        record = bool(source_ref and source_ref != 'memorize_tool' and '@post_response' not in source_ref)
        refs = [(m['id'], m.get('revision') or fingerprint(m)) for m in batch.messages] if batch else [(i, '') for i in evidence_ids(source_ref)]
        with self.memory_transaction():
            if batch:
                self.check_withdrawn(batch)
            previous = self._db.execute('SELECT result FROM memory_write_receipts WHERE id=?', (key,)).fetchone() if record else None
            if previous and action not in ('replace',):
                result = json.loads(previous[0])
                if isinstance(result, str) and ':' in result:
                    item = self.get_items_by_ids([result.split(':', 1)[1]])
                    if not item or item[0]['status'] != 'active':
                        return 'skipped:' + result.split(':', 1)[1]
                if action == 'add' and isinstance(result, str) and not result.startswith('skipped:'):
                    return 'existing:' + result.split(':', 1)[1]
                return result
            if action in ('add', 'replace', 'archive_event'):
                row = self._db.execute('SELECT id FROM memory_items WHERE content_hash=? AND memory_type=?',
                    (_content_hash(payload['summary'], payload['memory_type']), payload['memory_type'])).fetchone()
                existing = self.get_items_by_ids([row[0]])[0] if row else None
                if existing and not compatible(existing, extra):
                    raise ValueError('Memory content scope mismatch')
                if action == 'replace':
                    result = self.replace_items(source_ref=source_ref, extra=extra, **payload)
                elif action == 'archive_event':
                    # Occurrences with distinct evidence/times must not collapse by text.
                    result = self.upsert_consolidation_event(source_ref=source_ref, extra=extra,
                        **{k: v for k, v in payload.items() if k != 'memory_type'})
                elif existing and record and (same_evidence(source_ref, existing.get('source_ref'))
                        or any(same_evidence(source_ref, prior[0]) for prior in self._db.execute(
                          'SELECT DISTINCT source_ref FROM memory_evidence WHERE item_id=? AND active=1', (row[0],)))):
                    result = ('existing:' if existing['status'] == 'active' else 'skipped:') + existing['id']
                    if batch and existing['status'] == 'active' and same_evidence(source_ref, existing.get('source_ref')) and '#rev:' not in (existing.get('source_ref') or ''):
                        self._db.execute('UPDATE memory_items SET source_ref=? WHERE id=?', (source_ref, existing['id']))
                else:
                    result = self.upsert_item(source_ref=source_ref, extra=extra, **payload)
            elif action in ('forget', 'invalidate'):
                ids = list(dict.fromkeys(payload['ids']))
                if any(not isinstance(i, str) or i.startswith('notebook:') for i in ids):
                    raise ValueError('Expected Memory2 IDs')
                items = self.get_items_by_ids(ids)
                if any(not compatible(item, extra) for item in items):
                    raise ValueError('Memory target scope mismatch')
                result = [item['id'] for item in items]
                for item in items:
                    # Carry eligibility across multiple independent causes of retirement.
                    can_restore = item['status'] == 'active' or bool(self._db.execute(
                        "SELECT 1 FROM memory_retirements WHERE item_id=? AND active=1 AND restorable=1 UNION ALL "
                        "SELECT 1 FROM memory_replacements WHERE old_item_id=? AND relation_type='supersede' LIMIT 1",
                        (item['id'], item['id'])).fetchone())
                    self._db.execute('INSERT OR IGNORE INTO memory_retirements VALUES (?,?,?,?,1)',
                                     (item['id'], key, source_ref, int(can_restore)))
                self.mark_superseded_batch(result)
            else:
                raise ValueError('Unsupported memory change')
            if record:
                self._db.execute('INSERT OR IGNORE INTO memory_write_receipts VALUES (?,?,?,?)',
                    (key, source_ref, action, json.dumps(result, ensure_ascii=False)))
            # A replacement which reuses an existing target is a relationship,
            # not independent evidence for that target (P1 undo contract).
            if record and isinstance(result, str) and not result.startswith('skipped:') and not (action == 'replace' and row):
                item_id = result.split(':', 1)[1]
                self._db.executemany('INSERT OR IGNORE INTO memory_evidence VALUES (?,?,?,?,?,?,1)',
                    [(item_id, key, batch.root if batch else '', mid, rev, source_ref) for mid, rev in refs])
            return result

    def check_withdrawn(self, batch):
        for m in batch.messages:
            if self._db.execute("SELECT 1 FROM memory_coverage WHERE root=? AND message_id=? AND revision=? AND state='withdrawn'",
                                (batch.root, m['id'], m.get('revision') or fingerprint(m))).fetchone():
                raise MemoryTaskBlocked('source_withdrawn')

    def claim_extraction(self, root, message, job):
        """Freeze the decision in the task receipt; never retry another unknown job."""
        revision = (message.get('revision') or fingerprint(message))
        row = self._db.execute('SELECT state,job FROM memory_coverage WHERE root=? AND message_id=? AND revision=?',
            (root, message['id'], revision)).fetchone()
        if row and row[0] in ('written', 'empty', 'skipped'):
            return False
        if row and (row[0] in ('blocked', 'withdrawn') or (row[0] == 'processing' and row[1] != job)):
            return 'held'  # Other sources in this archive window may still proceed.
        self._db.execute('INSERT OR REPLACE INTO memory_coverage VALUES (?,?,?,?,?,?,?)',
            (root, message['id'], revision, 'processing', job, '', _now_iso()))
        self._db.commit()
        return True

    def finish_extraction(self, root, message, rows):
        batch = memory_sources.get()
        if batch:
            batch.validate()
        counts = {'new': 0, 'reinforced': 0, 'skipped': 0}
        from core.memory.sources import SourceBatch
        with self.memory_transaction():
            if batch is None or batch.root != root:
                raise MemoryTaskBlocked('source_identity_missing')
            self.check_withdrawn(batch)
            token = memory_sources.set(SourceBatch(batch.journal, [message], batch._validated_head))
            try:
                return self._finish_one(root, message, rows, counts)
            finally:
                memory_sources.reset(token)

    def _finish_one(self, root, message, rows, counts):
        with self.memory_transaction():
            for row in rows:
                result = self.commit_change('add', source_ref=message_ref([message], root), **row)
                status = result.split(':', 1)[0]
                counts[status] = counts.get(status, 0) + 1
            state = 'empty' if not rows else 'written' if counts['new'] + counts['reinforced'] + counts.get('existing', 0) else 'skipped'
            self._db.execute('UPDATE memory_coverage SET state=?,reason=?,updated_at=? WHERE root=? AND message_id=? AND revision=?',
                (state, json.dumps(counts), _now_iso(), root, message['id'], (message.get('revision') or fingerprint(message))))
        return counts

    def defer_extraction(self, root, messages, reason):
        for m in messages:
            if m.get('role') == 'user':
                self._db.execute('INSERT OR IGNORE INTO memory_coverage VALUES (?,?,?,?,?,?,?)',
                    (root, m['id'], (m.get('revision') or fingerprint(m)), 'deferred', '', reason, _now_iso()))
        self._db.commit()

    def block_extraction(self, job, reason):
        self._db.execute("UPDATE memory_coverage SET state='blocked',reason=?,updated_at=? WHERE job=? AND state='processing'",
            (reason, _now_iso(), job))
        self._db.commit()
