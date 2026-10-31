"""Explicitly opened, single-scope sandbox; no Host, provider or transport imports."""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .contracts import (
    Note,
    NotebookError as NotebookError,
    Source,
    digest,
    draft,
    encode,
    parse,
    require,
    text,
    timestamp,
)


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Notebook:
    FORMAT = "kaze-notebook-offline-v1"

    @classmethod
    def create(cls, directory, *, owner, scope, clock=utcnow):
        """Only a NEW explicit directory can become an experiment sandbox."""
        text(owner, "owner", 200)
        text(scope, "scope", 200)
        directory = Path(directory).resolve()
        require(not directory.exists(), "Sandbox directory already exists; use open")
        directory.mkdir(parents=True)
        metadata = {"format": cls.FORMAT, "owner": owner, "scope": scope}
        db = sqlite3.connect(directory / "notebook.sqlite3")
        try:
            db.executescript("""
                CREATE TABLE metadata(value TEXT NOT NULL);
                CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                    at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE runs(id TEXT PRIMARY KEY, request TEXT NOT NULL,
                    state TEXT NOT NULL, result TEXT);
            """)
            db.execute("INSERT INTO metadata VALUES (?)", (encode(metadata),))
            db.commit()
        finally:
            db.close()
        (directory / "sandbox.json").write_text(encode(metadata), encoding="utf-8")
        return cls(directory, owner=owner, scope=scope, clock=clock)

    def __init__(self, directory, *, owner, scope, clock=utcnow):
        self.directory = Path(directory).resolve()
        self.clock = clock
        expected = {"format": self.FORMAT, "owner": owner, "scope": scope}
        marker = self.directory / "sandbox.json"
        require(marker.is_file(), "Not a notebook sandbox")
        require(
            json.loads(marker.read_text(encoding="utf-8")) == expected,
            "Sandbox identity mismatch",
        )
        path = self.directory / "notebook.sqlite3"
        require(
            path.is_file() and not path.is_symlink(),
            "Sandbox database missing or linked",
        )
        self.db = sqlite3.connect(
            path.as_uri() + "?mode=rw", uri=True, isolation_level=None
        )
        self.db.row_factory = sqlite3.Row
        try:
            require(
                json.loads(self.db.execute("SELECT value FROM metadata").fetchone()[0])
                == expected,
                "Database identity mismatch",
            )
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=5000")
        except BaseException:
            self.db.close()
            raise
        self.identity = expected

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def events(self, kind=None):
        query = (
            "SELECT * FROM events" + (" WHERE kind=?" if kind else "") + " ORDER BY seq"
        )
        return [
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in self.db.execute(query, (kind,) if kind else ())
        ]

    def _event(self, identity, kind, payload):
        value = encode(payload)
        old = self.db.execute("SELECT * FROM events WHERE id=?", (identity,)).fetchone()
        if old:
            require(
                old["kind"] == kind and old["payload"] == value,
                "Operation identity conflict",
            )
            return old["seq"]
        return self.db.execute(
            "INSERT INTO events(id,kind,at,payload) VALUES (?,?,?,?)",
            (identity, kind, timestamp(self.clock()), value),
        ).lastrowid

    def revision(self):
        return max((e["seq"] for e in self.events("correction")), default=0)

    def sources(self):
        return {e["id"]: e["payload"] for e in self.events("source")}

    def import_sources(self, values):
        require(
            isinstance(values, list) and 0 < len(values) <= 50,
            "Import needs 1..50 sources",
        )
        sources = [parse(Source, v) for v in values]
        with self.transaction():
            for source in sources:
                self._event(source.ref, "source", asdict(source))
        return [s.ref for s in sources]

    def active_notes(self):
        retired = {
            n for e in self.events("correction") for n in e["payload"]["replaces"]
        }
        return {
            e["id"]: {**e["payload"], "recorded_at": e["at"]}
            for e in self.events("note")
            if e["id"] not in retired
        }

    def coverage(self):
        """Explicit per-source receipts, not a claim of semantic completeness."""
        return {
            ref: e["id"]
            for e in self.events("committed")
            if e["payload"]["role"] == "exploration"
            for ref in e["payload"]["source_refs"]
        }

    def prepare(self, run_id, role, source_refs=None, *, memory_background=None):
        text(run_id, "run id", 160)
        require(role in {"exploration", "judgement"}, "Invalid role")
        with self.transaction():
            old = self.db.execute(
                "SELECT request FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if old:
                request = json.loads(old[0])
                require(
                    request["role"] == role
                    and (
                        source_refs is None
                        or sorted(source_refs) == request["source_refs"]
                    ),
                    "Run identity conflict",
                )
                require(
                    memory_background is None
                    or request.get("memory_background") == memory_background,
                    "Run background identity conflict",
                )
                return request
            pending = self.db.execute(
                "SELECT id,request FROM runs WHERE state NOT IN ('committed','discarded')"
            ).fetchall()
            require(
                not any(json.loads(r["request"])["role"] == role for r in pending),
                "An unfinished run already owns this phase; reconcile it first",
            )
            sources, notes = self.sources(), self.active_notes()
            require(
                source_refs is None
                or (
                    isinstance(source_refs, list)
                    and all(isinstance(ref, str) for ref in source_refs)
                ),
                "Invalid source selection",
            )
            refs = sorted(
                source_refs
                if source_refs is not None
                else (
                    set(sources) - set(self.coverage()) if role == "exploration" else []
                )
            )
            require(
                len(set(refs)) == len(refs) and set(refs) <= set(sources),
                "Invalid source selection",
            )
            if role == "exploration":
                require(bool(refs), "No new input")
                require(not set(refs) & set(self.coverage()), "Sources already covered")
            require(len(refs) <= 50 and len(notes) <= 200, "Narrow the offline sample")
            context_refs = {e["ref"] for n in notes.values() for e in n["evidence"]}
            request = {
                "protocol": self.FORMAT,
                "run_id": run_id,
                "role": role,
                "identity": self.identity,
                "source_refs": refs,
                "sources": {
                    ref: sources[ref] for ref in sorted(set(refs) | context_refs)
                },
                "notes": notes,
                "correction_revision": self.revision(),
                "corrections": [e["payload"] for e in self.events("correction")],
                "created_at": timestamp(self.clock()),
            }
            if memory_background is not None:
                require(
                    isinstance(memory_background, dict)
                    and memory_background.get("mode") == "read_only_keyword_baseline",
                    "Unsupported background context",
                )
                request["memory_background"] = memory_background
            require(
                len(encode(request)) <= 60000,
                "Input too large; nothing truncated or submitted",
            )
            self.db.execute(
                "INSERT INTO runs VALUES (?,?,'prepared',NULL)",
                (run_id, encode(request)),
            )
            return request

    def run(self, run_id):
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        require(row is not None, "Unknown run")
        return {
            "id": row["id"],
            "state": row["state"],
            "request": json.loads(row["request"]),
            "result": json.loads(row["result"]) if row["result"] is not None else None,
        }

    def reserve_request(self, run_id):
        """Fences ambiguous submission; caller must save results, never auto-resubmit."""
        with self.transaction():
            run = self.run(run_id)
            require(
                run["state"] == "prepared",
                "Request already reserved; outcome needs reconciliation",
            )
            require(
                run["request"]["correction_revision"] == self.revision(),
                "Stale understanding",
            )
            self.db.execute(
                "UPDATE runs SET state='awaiting_result' WHERE id=?", (run_id,)
            )
            return run["request"]

    def record_result(self, run_id, result):
        # Persist even an invalid draft for diagnosis. Validation/commit are separate.
        value = encode(result)
        require(len(value) <= 60000, "Result too large")
        with self.transaction():
            run = self.run(run_id)
            require(
                run["state"] in {"awaiting_result", "result_recorded", "committed"},
                "Request not reserved",
            )
            if run["state"] != "awaiting_result":
                require(encode(run["result"]) == value, "Recorded result is immutable")
                return
            self.db.execute(
                "UPDATE runs SET result=?,state='result_recorded' WHERE id=?",
                (value, run_id),
            )

    def discard(self, run_id, reason):
        """Explicitly retire an unsubmitted or known-result draft, never an unknown call."""
        text(reason, "discard reason", 500)
        with self.transaction():
            run = self.run(run_id)
            require(
                run["state"] in {"prepared", "result_recorded", "discarded"},
                "Unknown request or committed work cannot be discarded",
            )
            self._event("discard:" + run_id, "discarded", {"reason": reason})
            self.db.execute("UPDATE runs SET state='discarded' WHERE id=?", (run_id,))

    def _validate_note(self, note, allowed_refs, topics):
        sources = self.sources()
        banned = {
            ref
            for e in self.events("correction")
            if e["payload"]["topic"] == note.topic
            for ref in e["payload"]["retired_refs"]
        }
        for evidence in note.evidence:
            ref = evidence["ref"]
            require(
                ref in allowed_refs and ref in sources,
                "Evidence outside captured input",
            )
            require(ref not in banned, "Corrected evidence cannot relearn this topic")
            require(
                evidence["quote"] in sources[ref]["text"], "Quote absent from source"
            )
            if note.kind == "statement":
                require(
                    sources[ref]["kind"] == "user",
                    "User statement requires user source",
                )
        require(
            all(r["topic"] in topics for r in note.related), "Unknown related topic"
        )
        for current in self.active_notes().values():
            if current["topic"] == note.topic:
                require(current["title"] == note.title, "Topic title conflict")

    def commit(self, run_id):
        with self.transaction():
            run = self.run(run_id)
            if run["state"] == "committed":
                return self._receipt(run_id)
            require(
                run["state"] == "result_recorded",
                "No durable result; do not rerun unknown request",
            )
            request, result = run["request"], run["result"]
            require(
                request["correction_revision"] == self.revision(), "Stale understanding"
            )
            notes, findings = draft(result)
            require(
                request["role"] == "judgement" or not (findings or result["withheld"]),
                "Exploration only records knowledge",
            )
            topics = {n["topic"] for n in self.active_notes().values()} | {
                n.topic for n in notes
            }
            for note in notes:
                self._validate_note(note, set(request["sources"]), topics)
                self._event(note.id, "note", asdict(note))
            used = {e["ref"] for n in notes for e in n.evidence} & set(
                request["source_refs"]
            )
            skipped = set(result["skipped"])
            require(
                not used & skipped and used | skipped == set(request["source_refs"]),
                "Every selected source needs a note or explicit skip reason",
            )
            active = self.active_notes()
            # A worker can cite captured context or notes it wrote, not another concurrent run.
            visible_notes = set(request["notes"]) | {n.id for n in notes}
            ids = []
            for finding in findings:
                require(
                    set(finding.note_ids) <= visible_notes & set(active),
                    "Finding has stale or unknown notes",
                )
                require(
                    any(active[n]["topic"] == finding.topic for n in finding.note_ids),
                    "Finding topic unsupported",
                )
                require(
                    datetime.fromisoformat(finding.expires_at)
                    > datetime.fromisoformat(self.clock()),
                    "Finding expired",
                )
                # Same evidence for the same topic is one candidate even if wording changes.
                fid = "finding-" + digest(
                    [finding.topic, sorted(finding.note_ids), self.revision()]
                )
                prior = self.db.execute(
                    "SELECT 1 FROM events WHERE id=?", (fid,)
                ).fetchone()
                if not prior:
                    self._event(
                        fid, "finding", {**asdict(finding), "revision": self.revision()}
                    )
                ids.append(fid)
            for index, decision in enumerate(result["withheld"]):
                require(decision["topic"] in topics, "Unknown withheld topic")
                self._event(f"withheld:{run_id}:{index}", "withheld", decision)
            receipt = {
                "role": request["role"],
                "source_refs": request["source_refs"],
                "note_ids": [n.id for n in notes],
                "finding_ids": ids,
                "skipped": result["skipped"],
            }
            self._event("commit:" + run_id, "committed", receipt)
            self.db.execute("UPDATE runs SET state='committed' WHERE id=?", (run_id,))
            return receipt

    def _receipt(self, run_id):
        row = self.db.execute(
            "SELECT payload FROM events WHERE id=?", ("commit:" + run_id,)
        ).fetchone()
        require(row is not None, "Commit receipt missing")
        return json.loads(row[0])

    def correct(self, operation_id, *, replaces, replacement):
        """Explicit host/user correction, never a permission granted to model drafts."""
        text(operation_id, "correction id", 160)
        note = parse(Note, replacement)
        payload = {"replaces": replaces, "replacement": replacement}
        with self.transaction():
            previous = next(
                (
                    e
                    for e in self.events("correction")
                    if e["id"] == "correction:" + operation_id
                ),
                None,
            )
            if previous:
                require(
                    previous["payload"]["input_digest"] == digest(payload),
                    "Correction identity conflict",
                )
                return previous["payload"]["note_id"]
            active = self.active_notes()
            require(
                isinstance(replaces, list)
                and bool(replaces)
                and all(isinstance(n, str) for n in replaces),
                "Correction targets required",
            )
            require(
                len(set(replaces)) == len(replaces) and set(replaces) <= set(active),
                "Correction targets must be active",
            )
            require(
                all(active[n]["topic"] == note.topic for n in replaces),
                "Correction must stay in one topic",
            )
            require(note.kind == "statement", "Correction requires user statement")
            self._validate_note(
                note, set(self.sources()), {n["topic"] for n in active.values()}
            )
            retired_refs = sorted(
                {e["ref"] for n in replaces for e in active[n]["evidence"]}
            )
            require(
                not {e["ref"] for e in note.evidence} & set(retired_refs),
                "Correction needs new evidence",
            )
            self._event(note.id, "note", asdict(note))
            self._event(
                "correction:" + operation_id,
                "correction",
                {
                    "input_digest": digest(payload),
                    "replaces": replaces,
                    "note_id": note.id,
                    "topic": note.topic,
                    "retired_refs": retired_refs,
                },
            )
            return note.id

    def preview(self):
        """Read only. No ACK, sender, score override, or publication claim."""
        active = self.active_notes()
        result = []
        for event in self.events("finding"):
            finding = event["payload"]
            if finding["revision"] != self.revision() or not set(
                finding["note_ids"]
            ) <= set(active):
                continue
            if datetime.fromisoformat(finding["expires_at"]) <= datetime.fromisoformat(
                self.clock()
            ):
                continue
            evidence = [e for n in finding["note_ids"] for e in active[n]["evidence"]]
            result.append(
                {
                    "id": event["id"],
                    "channel": "content",
                    "source": "notebook-preview",
                    "title": next(
                        active[n]["title"]
                        for n in finding["note_ids"]
                        if active[n]["topic"] == finding["topic"]
                    ),
                    "url": "",
                    "content": finding["text"],
                    "topic": finding["topic"],
                    "evidence": evidence,
                    "expires_at": finding["expires_at"],
                    "preview_only": True,
                }
            )
        return result

    def query(self, query="", *, limit=10):
        require(isinstance(query, str) and len(query) <= 2000, "Invalid query")
        require(type(limit) is int and 1 <= limit <= 50, "Invalid limit")
        terms = query.casefold().split()
        matches = []
        for identity, note in self.active_notes().items():
            haystack = encode(note).casefold()
            score = sum(term in haystack for term in terms)
            if not terms or score:
                matches.append((score, {"id": identity, **note}))
        matches.sort(key=lambda item: (-item[0], item[1]["id"]))
        return [note for _, note in matches[:limit]]

    def render(self):
        lines = [
            "# 联想记事本（隔离预览）",
            "",
            "内容是认识与线索，不是当前指令或已安排的任务。",
            "",
        ]
        for identity, note in self.active_notes().items():
            lines.extend(
                [
                    f"## {note['title']} · {note['topic']}",
                    f"- [{note['kind']}] {note['text']}",
                    f"- 发生：{note['occurred_at'] or '未知'}；记录：{note['recorded_at']}",
                    f"- 身份：{identity}",
                ]
            )
            for e in note["evidence"]:
                lines.append(f"- 来源：{e['ref']}；原文：{e['quote']}")
            for r in note["related"]:
                lines.append(f"- 关联：{r['topic']}；理由：{r['reason']}")
            lines.append("")
        lines.extend(["## 发现候选（尚未发送）", ""])
        lines.extend("- " + item["content"] for item in self.preview())
        return "\n".join(lines) + "\n"
