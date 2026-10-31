"""Strict, JSON-only boundary for externally produced notebook drafts."""

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
import hashlib
import json


class NotebookError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise NotebookError(message)


def encode(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def text(value, name, limit=4000):
    require(
        isinstance(value, str) and bool(value.strip()) and len(value) <= limit,
        f"Invalid {name}",
    )
    return value


def timestamp(value):
    text(value, "timestamp", 80)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise NotebookError("Invalid timestamp") from exc
    require(parsed.tzinfo is not None, "Timestamp requires timezone")
    return value


def parse(cls, value):
    require(isinstance(value, dict), f"{cls.__name__} must be an object")
    require(
        set(value) <= {f.name for f in fields(cls)}, f"Unknown {cls.__name__} fields"
    )
    try:
        return cls(**value)
    except TypeError as exc:
        raise NotebookError(f"Missing {cls.__name__} fields") from exc


def strings(value, name):
    require(isinstance(value, list) and len(value) <= 100, f"Invalid {name}")
    for item in value:
        text(item, name, 500)
    require(len(set(value)) == len(value), f"Duplicate {name}")


@dataclass(frozen=True)
class Source:
    id: str
    version: str
    kind: str
    text: str
    locator: str
    occurred_at: str | None = None

    def __post_init__(self):
        for name in ("id", "version", "locator"):
            text(getattr(self, name), name, 500)
        text(self.text, "source text", 16000)
        require(self.kind in {"user", "assistant", "material"}, "Invalid source kind")
        if self.occurred_at is not None:
            timestamp(self.occurred_at)

    @property
    def ref(self):
        return "source-" + digest([self.id, self.version])


@dataclass(frozen=True)
class Note:
    topic: str
    title: str
    text: str
    kind: str
    evidence: list[dict]
    related: list[dict] = field(default_factory=list)
    occurred_at: str | None = None

    def __post_init__(self):
        for name in ("topic", "title"):
            text(getattr(self, name), name, 160)
        text(self.text, "note", 2000)
        require(
            self.kind in {"statement", "observation", "inference", "question"},
            "Invalid note kind",
        )
        require(
            isinstance(self.evidence, list) and 0 < len(self.evidence) <= 20,
            "Evidence required",
        )
        for item in self.evidence:
            require(
                isinstance(item, dict) and set(item) == {"ref", "quote"},
                "Invalid evidence",
            )
            text(item["ref"], "source ref", 100)
            text(item["quote"], "quote", 2000)
        require(
            isinstance(self.related, list) and len(self.related) <= 20,
            "Invalid relations",
        )
        for item in self.related:
            require(
                isinstance(item, dict) and set(item) == {"topic", "reason"},
                "Relation requires reason",
            )
            text(item["topic"], "related topic", 160)
            text(item["reason"], "relation reason", 500)
            require(item["topic"] != self.topic, "Self relation")
        if self.occurred_at is not None:
            timestamp(self.occurred_at)

    @property
    def id(self):
        return "note-" + digest(asdict(self))


@dataclass(frozen=True)
class Finding:
    topic: str
    text: str
    note_ids: list[str]
    expires_at: str

    def __post_init__(self):
        text(self.topic, "finding topic", 160)
        text(self.text, "finding text", 3000)
        strings(self.note_ids, "note_ids")
        require(bool(self.note_ids), "Finding needs notes")
        timestamp(self.expires_at)


def draft(value):
    require(
        isinstance(value, dict)
        and set(value) == {"notes", "findings", "withheld", "skipped"},
        "Invalid draft fields",
    )
    for key in ("notes", "findings", "withheld"):
        require(
            isinstance(value[key], list) and len(value[key]) <= 50, f"Invalid {key}"
        )
    notes = [parse(Note, n) for n in value["notes"]]
    findings = [parse(Finding, n) for n in value["findings"]]
    require(isinstance(value["skipped"], dict), "Invalid skipped sources")
    for ref, reason in value["skipped"].items():
        text(ref, "skipped ref", 100)
        text(reason, "skip reason", 500)
    for item in value["withheld"]:
        require(
            isinstance(item, dict) and set(item) == {"topic", "reason"},
            "Invalid withheld decision",
        )
        text(item["topic"], "withheld topic", 160)
        text(item["reason"], "withheld reason", 500)
    return notes, findings
