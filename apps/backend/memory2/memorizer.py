"""
Memory v2 写入器：将 consolidation 结果保存到 SQLite
"""

from __future__ import annotations

import logging
import re

from memory2.store import MemoryStore2
from memory2.embedder import Embedder

logger = logging.getLogger(__name__)

_TIME_PREFIX_RE = re.compile(
    r"^\[(?P<date>\d{4}-\d{2}-\d{2})(?:[ T](?P<hour>\d{2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?\]"
)


def _coerce_emotional_weight(value: object) -> int:
    if value is None or value == "":
        return 0
    if not isinstance(value, str | int | float):
        return 0
    try:
        return max(0, min(10, int(value)))
    except (TypeError, ValueError):
        return 0


def _parse_history_entry_happened_at(summary: str) -> str | None:
    match = _TIME_PREFIX_RE.match((summary or "").strip())
    if not match:
        return None
    date = match.group("date")
    hour = match.group("hour") or "00"
    minute = match.group("minute") or "00"
    second = match.group("second") or "00"
    return f"{date}T{hour}:{minute}:{second}"


class Memorizer:
    def __init__(self, store: MemoryStore2, embedder: Embedder) -> None:
        self._store = store
        self._embedder = embedder

    async def save_item(
        self,
        summary: str,
        memory_type: str,
        extra: dict,
        source_ref: str,
        happened_at: str | None = None,
        emotional_weight: int = 0,
    ) -> str:
        """embed → content_hash → upsert，返回 'new:id' 或 'reinforced:id'"""
        embedding = await self._embedder.embed(summary)
        return self._store.commit_change("add",
            memory_type=memory_type,
            summary=summary,
            embedding=embedding,
            source_ref=source_ref,
            extra=extra,
            happened_at=happened_at,
            emotional_weight=emotional_weight,
        )

    async def save_item_with_supersede(
        self,
        summary: str,
        memory_type: str,
        extra: dict,
        source_ref: str,
        happened_at: str | None = None,
        emotional_weight: int = 0,
        supersedes: list[str] | None = None,
    ) -> str:
        """Ordinary writes coexist; only explicit IDs authorize replacement.

        Similarity is retrieval evidence, not a contradiction or merge verdict.
        The store commits the new item, replacement record and retirement together.
        """
        if not supersedes:
            return await self.save_item(
                summary, memory_type, extra, source_ref, happened_at, emotional_weight,
            )
        embedding = await self._embedder.embed(summary)
        return self._store.commit_change("replace",
            memory_type=memory_type, summary=summary, embedding=embedding,
            source_ref=source_ref, extra=extra, happened_at=happened_at,
            emotional_weight=emotional_weight, supersedes=supersedes,
        )

    async def save_from_consolidation(
        self,
        history_entry: str,
        behavior_updates: list[dict],
        source_ref: str,
        scope_channel: str,
        scope_chat_id: str,
        role_id: str = "",
        emotional_weight: int = 0,
    ) -> None:
        """将 consolidation 的产出写入 SQLite"""
        # 1. history_entry → event
        if history_entry and history_entry.strip():
            try:
                text = history_entry.strip()
                if self._store.has_consolidation_source_ref(source_ref):
                    logger.info(
                        "memory2 consolidation skip duplicated source_ref=%s",
                        source_ref,
                    )
                    text = ""
                if text:
                    embedding = await self._embedder.embed(text)
                    result = self._store.commit_change("archive_event", memory_type="event",
                        source_ref=source_ref,
                        summary=text,
                        embedding=embedding,
                        extra={
                            "role_id": role_id,
                            "scope_channel": scope_channel,
                            "scope_chat_id": scope_chat_id,
                        },
                        happened_at=_parse_history_entry_happened_at(text),
                        emotional_weight=emotional_weight,
                    )
                    if result.startswith("skipped:"):
                        logger.info(
                            "memory2 consolidation skip duplicated source_ref=%s",
                            source_ref,
                        )
                    else:
                        logger.info(f"memory2 event saved: {result}")
            except Exception as e:
                logger.warning(f"memory2 event save 失败: {e}")
                from core.memory.task_context import memory_task
                if memory_task.get() is not None:
                    raise

        # 2. behavior_updates 统一由 post-response worker 处理，避免与 consolidation 重复写入
        if behavior_updates:
            logger.info(
                "memory2 consolidation skip behavior_updates (%d): handled by post-response worker",
                len(behavior_updates),
            )

    def supersede_batch(self, ids: list[str], *, source_ref="", extra=None) -> None:
        self._store.commit_change("invalidate", ids=ids, source_ref=source_ref, extra=extra)

    def reinforce_items_batch(self, ids: list[str]) -> None:
        self._store.reinforce_items_batch(ids)

    async def merge_item(
        self,
        item_id: str,
        merged_summary: str,
        extra_patch: dict | None = None,
    ) -> None:
        """原子更新 merge 目标：summary + content_hash + embedding + extra_json。
        对 procedure 类型同步重建 rule_schema，并写入 _merge_note 供溯源。
        调用方保证 merged_summary 非空且 item_id 存在。
        """
        from memory2.store import _content_hash

        merged_summary = (merged_summary or "").strip()
        if not merged_summary or not item_id:
            return

        rows = self._store.get_items_by_ids([item_id])
        if not rows:
            logger.warning("merge_item: item %s not found", item_id)
            return

        memory_type = str(rows[0]['memory_type'])
        raw_extra = rows[0].get('extra_json')
        old_extra: dict = raw_extra if isinstance(raw_extra, dict) else {}

        new_embedding = await self._embedder.embed(merged_summary)
        new_hash = _content_hash(merged_summary, memory_type)

        # 构建更新后的 extra_json
        new_extra = dict(old_extra)
        new_extra["_merge_note"] = merged_summary   # 溯源：记录 merge 时的摘要
        if extra_patch:
            if extra_patch.get("tool_requirement"):
                new_extra["tool_requirement"] = extra_patch.get("tool_requirement")
            if extra_patch.get("steps"):
                merged_steps: list[str] = []
                seen_steps: set[str] = set()
                for step in (old_extra.get("steps") or []) + (extra_patch.get("steps") or []):
                    text = str(step or "").strip()
                    if not text or text in seen_steps:
                        continue
                    seen_steps.add(text)
                    merged_steps.append(text)
                new_extra["steps"] = merged_steps
        if memory_type == "procedure":
            from memory2.rule_schema import build_procedure_rule_schema
            new_extra["rule_schema"] = build_procedure_rule_schema(
                summary=merged_summary,
                tool_requirement=new_extra.get("tool_requirement"),
                steps=new_extra.get("steps") or [],
                rule_schema=old_extra.get("rule_schema"),
            )
            # trigger_tags 依赖 tagger（需要 LLM），merge 时无法在线重建。
            # 保留旧标签比没有标签更危险（可能触发错误的关键词匹配），故直接清除。
            # 下次通过 _save_item_direct 路径写入时，tagger 会重新生成。
            new_extra.pop("trigger_tags", None)

        self._store.merge_item_raw(
            item_id=item_id,
            new_summary=merged_summary,
            new_hash=new_hash,
            new_embedding=new_embedding,
            new_extra=new_extra,
        )
        logger.info("memorizer merge_item id=%s", item_id)
