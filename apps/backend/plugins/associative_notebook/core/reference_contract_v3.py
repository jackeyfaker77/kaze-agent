"""Opt-in offline reference contract. No provider, schema migration or Host wiring."""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
import hashlib
from pathlib import Path
import re

from .contracts import NotebookError, digest, draft, require
from .source_contract_v2 import request_envelope as source_envelope
from .source_contract_v2 import manifest as source_manifest
from .source_contract_v2 import SOURCE_RULES
from .store import Notebook as LegacyNotebook

VERSION = "kaze-notebook-reference-v3"
REFERENCE_RULES = """引用契约（探索与判断均可追加 notes；只有判断可输出 findings/withheld）：
sources 的键是来源 ref；输入 notes 的键是正式笔记 ID；notes[*].topic 是主题 ID。这三个命名空间不能互换，也不要使用 memory_background 中无法回源的标识。
notes[*].evidence[*].ref 必须逐字取自输入 sources 的键，quote 必须是该来源原文中的逐字片段；statement 的来源必须是 user。纠正已退役的来源不能用来重新学习原主题。
已有主题只以输入 notes 为准；同批新主题必须由本批有效的 notes 声明，不能只在 findings、withheld 或 related 中凭空创建。复用主题时 title 必须保持固定，同批相同主题也必须使用同一 title。
notes[*].related[*].topic 只能引用输入 notes 的主题或本批 notes 声明的主题，不可关联自身；必须提供 reason。同批前向关联及相互关联允许。
findings[*].note_ids 的每一项必须是输入 notes 的完整键，或本批笔记引用 new:N（N 为 notes 数组从 0 开始的十进制下标，例如 new:0；不得有符号、空格或多余前导零）。仅 Host 将 new:N 解析为正式 ID，模型无需计算哈希或在 notes 中输出 id。别名仅在本批有效，不可用于 topic、evidence.ref 或 skipped；引用不得重复，包括别名与正式 ID 解析后指向同一笔记的情况。
findings[*].topic 必须与该 finding 的 note_ids 所引用的至少一条笔记的 topic 完全一致；不是自由描述或新标签。expires_at 必须带时区且晚于本轮当前时间。findings 仍只是待审候选。
withheld[*].topic 也必须是输入 notes 的主题或本批有效 notes 声明的主题；reason 解释暂不提出该主题。没有合适主题时允许 withheld=[]，不要为没有证据的背景或不成立的联想虚构主题，也不要为了填写 withheld 创造无依据的笔记。
引用以本轮捕获的输入为边界，提交前若被纠正或失效则整批拒绝。提交按整批事务处理；任何错误都保留原始响应并返回可判断的错误位置，不静默删除、改写或自动重试。"""


def manifest():
    return {
        "version": VERSION,
        "sources": {
            **source_manifest()["sources"],
            "reference_contract_v3.py": hashlib.sha256(
                Path(__file__).read_bytes()
            ).hexdigest(),
        },
        "rules_digest": digest(REFERENCE_RULES),
        "storage_format": LegacyNotebook.FORMAT,
        "provider_execution_authorized": False,
    }


def upgrade_envelope(envelope):
    require("reference_contract" not in envelope, "Reference contract already bound")
    require(
        envelope.get("source_contract") == source_manifest(), "Expected v2 envelope"
    )
    require(
        envelope["system"].count(SOURCE_RULES) == 1, "Expected one v2 source-rule block"
    )
    require(
        envelope["request_digest"] == digest(envelope["input"]),
        "Request digest mismatch",
    )
    require(
        REFERENCE_RULES not in envelope["system"], "Reference rules already present"
    )
    value = deepcopy(envelope)
    value["system"] += "\n" + REFERENCE_RULES
    value["reference_contract"] = manifest()
    return value


def request_envelope(request):
    return upgrade_envelope(source_envelope(request))


class ContractErrors(NotebookError):
    """All independently checkable errors; no source quotes or model text in messages."""

    def __init__(self, errors):
        self.errors = tuple(errors)
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors))


def pointer(value):
    return str(value).replace("~", "~0").replace("/", "~1")


class Notebook(LegacyNotebook):
    def _plan(self, request, result):
        """Read-only validation inside the commit transaction; never alter the draft."""
        errors = []

        def check(ok, path, code, message):
            if not ok:
                errors.append({"path": path, "code": code, "message": message})
            return ok

        empty = {"notes": [], "findings": [], "withheld": [], "skipped": {}}
        if not check(isinstance(result, dict), "", "shape", "Invalid draft fields"):
            raise ContractErrors(errors)
        check(set(result) == set(empty), "", "shape", "Invalid draft fields")
        parsed = {}
        # Isolate malformed items so one bad object does not hide other references.
        for key in ("notes", "findings", "withheld"):
            items = result.get(key)
            parsed[key] = {}
            if not check(
                isinstance(items, list) and len(items) <= 50,
                "/" + key,
                "shape",
                "Invalid " + key,
            ):
                continue
            for i, item in enumerate(items):
                try:
                    ns, fs = draft({**empty, key: [item]})
                    parsed[key][i] = (
                        ns[0]
                        if key == "notes"
                        else fs[0]
                        if key == "findings"
                        else item
                    )
                except NotebookError as exc:
                    check(False, f"/{key}/{i}", "shape", str(exc))
        skipped = result.get("skipped")
        if not check(
            isinstance(skipped, dict), "/skipped", "shape", "Invalid skipped sources"
        ):
            skipped = {}
        for ref, reason in skipped.items():
            try:
                draft({**empty, "skipped": {ref: reason}})
            except NotebookError as exc:
                check(False, "/skipped/" + pointer(ref), "shape", str(exc))
        notes = parsed["notes"]
        findings = parsed["findings"]
        for key in ("findings", "withheld"):
            check(
                request["role"] == "judgement" or not result.get(key),
                "/" + key,
                "role",
                "Exploration only records knowledge",
            )

        active = self.active_notes()
        visible = {k: v for k, v in active.items() if k in request["notes"]}
        captured_topics = {n["topic"] for n in visible.values()}
        active_topics = {n["topic"] for n in active.values()}
        topics = captured_topics | {n.topic for n in notes.values()}
        titles = {n["topic"]: n["title"] for n in active.values()}
        sources = self.sources()
        corrections = self.events("correction")
        for i, note in notes.items():
            base = f"/notes/{i}"
            check(
                note.topic not in active_topics - captured_topics,
                base + "/topic",
                "unseen_topic",
                "Topic introduced outside captured input",
            )
            check(
                note.topic not in titles or titles[note.topic] == note.title,
                base + "/title",
                "title_conflict",
                "Topic title conflict",
            )
            titles.setdefault(note.topic, note.title)
            banned = {
                r
                for e in corrections
                if e["payload"]["topic"] == note.topic
                for r in e["payload"]["retired_refs"]
            }
            for j, evidence in enumerate(note.evidence):
                path = f"{base}/evidence/{j}"
                ref = evidence["ref"]
                if check(
                    ref in request["sources"] and ref in sources,
                    path + "/ref",
                    "source_outside_input",
                    "Evidence outside captured input",
                ):
                    check(
                        ref not in banned,
                        path + "/ref",
                        "retired_source",
                        "Corrected evidence cannot relearn this topic",
                    )
                    check(
                        evidence["quote"] in sources[ref]["text"],
                        path + "/quote",
                        "quote_absent",
                        "Quote absent from source",
                    )
                    check(
                        note.kind != "statement" or sources[ref]["kind"] == "user",
                        path + "/ref",
                        "source_kind",
                        "User statement requires user source",
                    )
            for j, relation in enumerate(note.related):
                check(
                    relation["topic"] in topics,
                    f"{base}/related/{j}/topic",
                    "unknown_topic",
                    "Unknown related topic",
                )
        selected = set(request["source_refs"])
        used = {e["ref"] for n in notes.values() for e in n.evidence} & selected
        for code, refs, message in (
            (
                "unselected_skipped",
                set(skipped) - selected,
                "Skipped refs outside selected source_refs",
            ),
            (
                "duplicate_coverage",
                used & set(skipped),
                "Selected source covered by both notes and skipped",
            ),
            (
                "missing_coverage",
                selected - (used | set(skipped)),
                "Selected source missing note or skip reason",
            ),
        ):
            for ref in sorted(refs):
                check(False, "/skipped/" + pointer(ref), code, message)

        visible.update({n.id: asdict(n) for n in notes.values()})
        aliases = {f"new:{i}": n.id for i, n in notes.items()}
        resolved = []
        for i, finding in findings.items():
            ids = []
            for j, raw in enumerate(finding.note_ids):
                path = f"/findings/{i}/note_ids/{j}"
                if raw.startswith("new:"):
                    valid = re.fullmatch(r"new:(0|[1-9][0-9]*)", raw) and raw in aliases
                    if not check(
                        valid,
                        path,
                        "invalid_batch_reference",
                        "Invalid or unavailable batch note reference",
                    ):
                        continue
                    identity = aliases[raw]
                else:
                    identity = raw
                if check(
                    identity in visible,
                    path,
                    "unknown_note",
                    "Finding has stale or unknown notes",
                ):
                    check(
                        identity not in ids,
                        path,
                        "duplicate_note",
                        "Duplicate resolved note_ids",
                    )
                    ids.append(identity)
            check(
                any(visible[n]["topic"] == finding.topic for n in ids),
                f"/findings/{i}/topic",
                "unsupported_topic",
                "Finding topic unsupported",
            )
            check(
                datetime.fromisoformat(finding.expires_at)
                > datetime.fromisoformat(self.clock()),
                f"/findings/{i}/expires_at",
                "expired",
                "Finding expired",
            )
            # Only build valid dataclasses after all errors have been collected.
            resolved.append((finding, ids))
        for i, decision in parsed["withheld"].items():
            check(
                decision["topic"] in topics,
                f"/withheld/{i}/topic",
                "unknown_topic",
                "Unknown withheld topic",
            )
        if errors:
            raise ContractErrors(errors)
        return (
            list(notes.values()),
            [replace(f, note_ids=ids) for f, ids in resolved],
            aliases,
        )

    def commit(self, run_id):
        # Keep validation and writes under the same SQLite writer lock. No preflight
        # followed by a weaker legacy commit, and no rewriting the durable response.
        with self.transaction():
            run = self.run(run_id)
            if run["state"] == "committed":
                return self._receipt(run_id)
            require(
                run["state"] == "result_recorded",
                "No durable result; do not rerun unknown request",
            )
            request, result = run["request"], run["result"]
            revision = self.revision()
            require(request["correction_revision"] == revision, "Stale understanding")
            notes, findings, aliases = self._plan(request, result)
            for note in notes:
                self._event(note.id, "note", asdict(note))
            ids = []
            for finding in findings:
                identity = "finding-" + digest(
                    [finding.topic, sorted(finding.note_ids), revision]
                )
                if not self.db.execute(
                    "SELECT 1 FROM events WHERE id=?", (identity,)
                ).fetchone():
                    self._event(
                        identity, "finding", {**asdict(finding), "revision": revision}
                    )
                ids.append(identity)
            for i, decision in enumerate(result["withheld"]):
                self._event(f"withheld:{run_id}:{i}", "withheld", decision)
            receipt = {
                "role": request["role"],
                "source_refs": request["source_refs"],
                "note_ids": [n.id for n in notes],
                "finding_ids": ids,
                "skipped": result["skipped"],
                "reference_contract": VERSION,
                "batch_note_refs": aliases,
            }
            self._event("commit:" + run_id, "committed", receipt)
            self.db.execute("UPDATE runs SET state='committed' WHERE id=?", (run_id,))
            return receipt
