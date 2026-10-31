"""Opt-in offline source-scope contract; frozen v1 and provider runners stay intact."""

from copy import deepcopy
import hashlib
from pathlib import Path

from .contracts import NotebookError, digest, draft, require
from .store import Notebook as LegacyNotebook
from .workflow import request_envelope as legacy_request_envelope

VERSION = "kaze-notebook-source-scope-v2"
LEGACY_RULES = """withheld 项：topic,reason。skipped 是本次未形成认识的 source_refs 到简短原因的映射。
每个本次 source_ref 都应有笔记引用或 skipped 原因。旧背景来源可以引用，不必再次整理。"""
SOURCE_RULES = """withheld 项：topic,reason。
source_refs 是本轮选中、必须交代处理结果的 ref 列表。sources 是可引用原文目录，除这些选中材料外，还可能包含既有 notes 回源所需的上下文；sources 中出现某个 ref 不表示本轮选中了它。
skipped 的每个键必须属于 source_refs。对 source_refs 中的每一项，要么由本轮新增 notes 的 evidence 覆盖，要么写入 skipped 并说明原因，不得遗漏或两者重复。findings 引用旧笔记不替代该处理回执。
当 source_refs 为空时，skipped 必须是空对象 {}。这时仍可从已有 notes 及其原文上下文形成 findings；不要把上下文 ref 填入 skipped，也不要为覆盖上下文而重复写 notes。memory_background 仍是另行提供的派生背景，不得冒充 sources 中的原文证据。"""


def manifest():
    directory = Path(__file__).resolve().parent
    return {
        "version": VERSION,
        "sources": {
            name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in (
                "source_contract_v2.py",
                "workflow.py",
                "store.py",
                "contracts.py",
            )
        },
        "rules_digest": digest(SOURCE_RULES),
        "storage_format": LegacyNotebook.FORMAT,
        "provider_execution_authorized": False,
    }


def upgrade_envelope(envelope):
    """Replace the ambiguous block only; preserve material bytes and other instructions."""
    require("source_contract" not in envelope, "Source contract already bound")
    require(
        envelope["request_digest"] == digest(envelope["input"]),
        "Request digest mismatch",
    )
    require(
        envelope["system"].count(LEGACY_RULES) == 1,
        "Expected one frozen source-rule block",
    )
    require(SOURCE_RULES not in envelope["system"], "Source rules already present")
    value = deepcopy(envelope)
    value["system"] = value["system"].replace(LEGACY_RULES, SOURCE_RULES, 1)
    value["source_contract"] = manifest()
    return value


def request_envelope(request):
    return upgrade_envelope(legacy_request_envelope(request))


class SourceCoverageError(NotebookError):
    """Stable error codes and ref IDs; do not include user material in the message."""

    def __init__(self, code, refs):
        self.code = code
        self.refs = tuple(sorted(refs))
        descriptions = {
            "unselected_skipped": "Skipped refs outside selected source_refs",
            "duplicate_coverage": "Selected source covered by both notes and skipped",
            "missing_coverage": "Selected source missing note or skip reason",
        }
        super().__init__(f"{descriptions[code]} ({len(self.refs)})")


def validate_coverage(request, result):
    """Same accepted coverage sets as v1; detailed reasons and no normalization."""
    notes, _ = draft(result)
    selected = set(request["source_refs"])
    used = {e["ref"] for note in notes for e in note.evidence} & selected
    skipped = set(result["skipped"])
    for code, refs in (
        ("unselected_skipped", skipped - selected),
        ("duplicate_coverage", used & skipped),
        ("missing_coverage", selected - (used | skipped)),
    ):
        if refs:
            raise SourceCoverageError(code, refs)


class Notebook(LegacyNotebook):
    """Explicit v2 import required. Schema, ownership and atomic commit remain v1."""

    def commit(self, run_id):
        run = self.run(run_id)
        if run["state"] == "result_recorded":
            validate_coverage(run["request"], run["result"])
        # Diagnostics do not write. The original transaction rechecks all invariants,
        # including coverage, before committing; no check is bypassed or relaxed.
        return super().commit(run_id)
