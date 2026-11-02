"""默认记忆引擎的管理与撤销操作。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import cast

from memory2.store import MemoryStore2


def _source_ref_message_ids(source_ref: str) -> list[str]:
    raw = str(source_ref or "").strip()
    if not raw:
        return []
    base = raw.split("#", 1)[0].strip()
    if not base.startswith("["):
        return []
    try:
        loaded: object = json.loads(base)
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    values: list[str] = []
    for item in cast(list[object], loaded):
        text = str(item).strip()
        if text:
            values.append(text)
    return values


def _undo_store_by_message_sources(
    store: MemoryStore2,
    message_ids: list[str],
    *,
    dry_run: bool = False,
    resolve_legacy=None, journal=None,
) -> dict[str, object]:
    target_ids = {str(item).strip() for item in message_ids if str(item).strip()}
    if not target_ids:
        return {"affected_ids": [], "restored_ids": [], "rollback_source_ids": []}
    rollback_source_ids: set[str] = set()

    def matches(source_ref):
        source = str(source_ref or "").strip()
        base_ids = _source_ref_message_ids(source)
        if resolve_legacy and source.startswith(("memory-task:", "markdown-task:")):
            base_ids = resolve_legacy(source)
        if source in target_ids:
            rollback_source_ids.add(source)
            return True
        if target_ids.intersection(base_ids):
            rollback_source_ids.update(base_ids)
            return True
        return False

    with store._lock:
        db = store._db
        affected_ids = {
            str(item_id)
            for item_id, source in db.execute(
                "SELECT id, source_ref FROM memory_items"
            ).fetchall()
            if matches(source)
        }
        # Each contribution may depend on several messages. Withdrawing any
        # invalidates that contribution, not other independent confirmations.
        evidence = db.execute("SELECT item_id,contribution,message_id,source_ref FROM memory_evidence WHERE active=1").fetchall()
        removed = {key for _, key, mid, _ in evidence if mid in target_ids}
        touched = {item for item, key, _, _ in evidence if key in removed}
        candidates = touched | affected_ids
        live_refs = {item: ref for item, key, _, ref in evidence if item in candidates and key not in removed}
        for item in touched:
            original = db.execute("SELECT source_ref FROM memory_items WHERE id=?", (item,)).fetchone()
            if original and not matches(original[0]) and original[0]:
                live_refs.setdefault(item, original[0])
        affected_ids.update(touched)
        affected_ids.difference_update(live_refs)
        # A reused target keeps its original source. The correction's source
        # lives on the relationship and must be undone independently of it.
        relation_ids = {
            int(row_id)
            for row_id, new_id, source in db.execute(
                "SELECT id,new_item_id,source_ref FROM memory_replacements "
                "WHERE relation_type='supersede'"
            ).fetchall()
            if matches(source) or str(new_id) in affected_ids
        }
        causes = db.execute('SELECT item_id,receipt,source_ref,restorable FROM memory_retirements WHERE active=1').fetchall()
        revoke = {receipt for _, receipt, ref, _ in causes if matches(ref)}
        keep_retired = {item for item, receipt, _, _ in causes if receipt not in revoke}
        eligible = {item for item, receipt, _, allowed in causes if receipt in revoke and allowed}
        outer_defer = db.defer_commit
        db.execute("SAVEPOINT memory_source_undo")
        db.defer_commit = True
        try:
            restored_ids = _restore_replacements_for_undo(
                store,
                affected_ids,
                dry_run=dry_run,
                relation_ids=relation_ids, eligible=eligible, keep_retired=keep_retired,
            )
            if not dry_run:
                now = datetime.now().astimezone().isoformat()
                db.executemany("UPDATE memory_retirements SET active=0 WHERE receipt=?", [(r,) for r in revoke])
                db.executemany("UPDATE memory_evidence SET active=0 WHERE contribution=?", [(key,) for key in removed])
                db.executemany("UPDATE memory_items SET source_ref=? WHERE id=?", [(ref, item) for item, ref in live_refs.items()])
                if journal is not None:
                    from core.memory.sources import read_messages, fingerprint
                    for msg in read_messages(journal, ids=sorted(target_ids)):
                        db.execute("INSERT OR REPLACE INTO memory_coverage VALUES (?,?,?,'withdrawn','','source_undo',?)",
                                   (journal.root_id, msg['id'], fingerprint(msg), now))
                db.executemany("UPDATE memory_coverage SET state='withdrawn',updated_at=? WHERE message_id=?", [(now, i) for i in target_ids])
                db.executemany(
                    "UPDATE memory_items SET status='superseded', updated_at=?, "
                    "extra_json=json_set(COALESCE(extra_json, '{}'), '$._source_undone', 1) WHERE id=?",
                    [(now, item_id) for item_id in sorted(affected_ids)],
                )
                db.executemany(
                    "UPDATE memory_replacements SET relation_type='supersede_undone' WHERE id=?",
                    [(row_id,) for row_id in sorted(relation_ids)],
                )
            db.execute("RELEASE memory_source_undo")
        except BaseException:
            db.execute("ROLLBACK TO memory_source_undo")
            db.execute("RELEASE memory_source_undo")
            raise
        finally:
            db.defer_commit = outer_defer
    return {
        "affected_ids": sorted(affected_ids),
        "restored_ids": sorted(restored_ids),
        "rollback_source_ids": sorted(rollback_source_ids),
    }


def _restore_replacements_for_undo(
    store: MemoryStore2,
    affected_ids: set[str],
    *,
    dry_run: bool = False,
    relation_ids: set[int] | None = None, eligible=None, keep_retired=None,
) -> set[str]:
    relations = store._db.execute(
        "SELECT id,old_item_id,new_item_id,relation_type FROM memory_replacements "
        "WHERE relation_type IN ('supersede', 'supersede_undone')"
    ).fetchall()
    if relation_ids is None:
        relation_ids = {
            r[0] for r in relations if r[2] in affected_ids and r[3] == "supersede"
        }
    candidates = {r[1] for r in relations if r[0] in relation_ids}
    statuses = dict(store._db.execute("SELECT id,status FROM memory_items").fetchall())
    withdrawn = {
        row[0]
        for row in store._db.execute(
            "SELECT id FROM memory_items WHERE json_extract(extra_json, '$._source_undone')=1"
        ).fetchall()
    }
    removed_sources = affected_ids | withdrawn
    predecessors: dict[str, set[str]] = {}
    successors: dict[str, set[str]] = {}
    for row_id, old_id, new_id, kind in relations:
        if new_id in removed_sources:
            predecessors.setdefault(new_id, set()).add(old_id)
        # Revoked edges only bridge withdrawn intermediate nodes. A revoked
        # correction to an independently existing target must not block undo.
        if (
            kind == "supersede" and row_id not in relation_ids
        ) or new_id in removed_sources:
            successors.setdefault(old_id, set()).add(new_id)
    pending = list(candidates)
    while pending:
        for ancestor in predecessors.get(pending.pop(), ()):
            if ancestor not in candidates:
                candidates.add(ancestor)
                pending.append(ancestor)
    candidates.update(eligible or ())
    candidates -= removed_sources | (keep_retired or set())

    def has_live_successor(item_id):
        pending = list(successors.get(item_id, ()))
        seen = {item_id}
        while pending:
            child = pending.pop()
            if child in seen:
                continue
            seen.add(child)
            if child not in affected_ids and statuses.get(child) == "active":
                return True
            pending.extend(successors.get(child, ()))
        return False

    restored = {
        item_id
        for item_id in candidates
        if item_id not in withdrawn
        and statuses.get(item_id) == "superseded"
        and not has_live_successor(item_id)
    }
    if not dry_run:
        now = datetime.now().astimezone().isoformat()
        store._db.executemany(
            "UPDATE memory_items SET status='active', updated_at=? WHERE id=? AND status='superseded'",
            [(now, item_id) for item_id in sorted(restored)],
        )
    return restored


class _AdminMixin:
    """提供记忆管理、批量操作与撤销能力。"""

    def reinforce_items_batch(self, ids: list[str]) -> None:
        if self._memorizer is not None:
            self._memorizer.reinforce_items_batch(ids)

    def keyword_match_procedures(
        self,
        action_tokens: list[str],
    ) -> list[dict[str, object]]:
        store = self._v2_store
        return (
            store.keyword_match_procedures(action_tokens) if store is not None else []
        )

    def list_events_by_time_range(
        self,
        time_start: datetime,
        time_end: datetime,
        *,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        store = self._v2_store
        if store is None:
            return []
        return store.list_events_by_time_range(time_start, time_end, limit=limit)

    def list_items_for_admin(
        self,
        *,
        q: str = "",
        memory_type: str = "",
        memory_domain: str = "",
        status: str = "",
        source_ref: str = "",
        role_id: str = "",
        scope_channel: str = "",
        scope_chat_id: str = "",
        has_embedding: bool | None = None,
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[dict[str, object]], int]:
        store = self._require_v2_store()
        return store.list_items_for_admin(
            q=q,
            memory_type=memory_type,
            memory_domain=memory_domain,
            status=status,
            source_ref=source_ref,
            role_id=role_id,
            scope_channel=scope_channel,
            scope_chat_id=scope_chat_id,
            has_embedding=has_embedding,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )

    def get_item_for_admin(
        self,
        item_id: str,
        *,
        include_embedding: bool = False,
    ) -> dict[str, object] | None:
        return self._require_v2_store().get_item_for_admin(
            item_id,
            include_embedding=include_embedding,
        )

    def update_item_for_admin(
        self,
        item_id: str,
        *,
        status: str | None = None,
        extra_json: dict[str, object] | None = None,
        source_ref: str | None = None,
        happened_at: str | None = None,
        emotional_weight: int | None = None,
    ) -> dict[str, object] | None:
        return self._require_v2_store().update_item_for_admin(
            item_id,
            status=status,
            extra_json=extra_json,
            source_ref=source_ref,
            happened_at=happened_at,
            emotional_weight=emotional_weight,
        )

    def delete_item(self, item_id: str) -> bool:
        return self._require_v2_store().delete_item(item_id)

    def delete_items_batch(self, ids: list[str]) -> int:
        return self._require_v2_store().delete_items_batch(ids)

    def undo_by_message_sources(
        self,
        message_ids: list[str],
        *,
        dry_run: bool = False,
    ) -> dict[str, object]:
        return _undo_store_by_message_sources(
            self._require_v2_store(),
            message_ids,
            dry_run=dry_run, journal=getattr(getattr(self, "memory_tasks", None), "journal", None), resolve_legacy=getattr(getattr(self, "memory_tasks", None), "legacy_sources", None),
        )

    def find_similar_items_for_admin(
        self,
        item_id: str,
        *,
        top_k: int = 8,
        memory_type: str = "",
        score_threshold: float = 0.0,
        include_superseded: bool = False,
    ) -> list[dict[str, object]]:
        return self._require_v2_store().find_similar_items_for_admin(
            item_id,
            top_k=top_k,
            memory_type=memory_type,
            score_threshold=score_threshold,
            include_superseded=include_superseded,
        )
