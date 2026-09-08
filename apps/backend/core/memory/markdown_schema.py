"""Canonical global-memory Markdown schemas and legacy migration helpers."""

from __future__ import annotations

from pathlib import Path

DOCUMENT_DEFAULTS = {
    "SELF.md": """# 助手工作说明

## 工作方式
- 根据用户请求和事实完成任务。

## 用户信息
- 只保留用户明确表达或确认的信息。

## 协作约定
- 不虚构经历；不确定时说明依据和缺失信息。
""",
    "MEMORY.md": """# 我的长期记忆

## 关于你

## 你的偏好

## 你希望我记住的事
""",
    "HISTORY.md": "# 我们的共同经历\n",
    "RECENT_CONTEXT.md": """# 最近发生的事

## 最近聊过的事
- none

## 还在继续的事
- none

## 最近的对话
<!-- a-preview = assistant reply preview only -->
- none
""",
    "PENDING.md": "# 待整理的记忆\n",
}

DOCUMENT_SECTIONS = {
    "SELF.md": (
        "## 工作方式",
        "## 用户信息",
        "## 协作约定",
    ),
    "MEMORY.md": (
        "## 关于你",
        "## 你的偏好",
        "## 你希望我记住的事",
    ),
}

def ensure_memory_documents(memory_dir: Path) -> None:
    """Creates missing global-memory documents without rewriting existing content."""

    memory_dir.mkdir(parents=True, exist_ok=True)
    for filename, default in DOCUMENT_DEFAULTS.items():
        path = memory_dir / filename
        if not path.exists():
            path.write_text(default, encoding="utf-8")


def normalize_memory_document(filename: str, content: str) -> str:
    """Validates a supported memory document and normalizes line endings."""

    if filename not in DOCUMENT_DEFAULTS:
        raise ValueError(f"unsupported memory document: {filename}")
    text = str(content or "").replace("\r\n", "\n").strip()
    if not text:
        return DOCUMENT_DEFAULTS[filename].rstrip() + "\n"

    return text + "\n"


def pending_body(content: str) -> str:
    """Returns pending items without the canonical document title."""

    lines = str(content or "").splitlines()
    if lines and lines[0].strip() == "# 待整理的记忆":
        lines = lines[1:]
    return "\n".join(lines).strip()


def replace_memory_section(path: Path, heading: str, body: str) -> None:
    """Replaces one canonical SELF or MEMORY section without touching its peers."""

    filename = path.name
    sections = DOCUMENT_SECTIONS.get(filename)
    if sections is None or heading not in sections:
        raise ValueError(f"unsupported memory section: {filename} {heading}")
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = normalize_memory_document(filename, current).rstrip().splitlines()
    if heading not in lines:
        clean_body = str(body or "").strip()
        appended = [heading]
        if clean_body:
            appended.extend(["", *clean_body.splitlines()])
        path.write_text(
            "\n".join([*lines, "", *appended]).strip() + "\n",
            encoding="utf-8",
        )
        return

    start = lines.index(heading)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index] in sections:
            end = index
            break
    clean_body = str(body or "").strip()
    replacement = [heading]
    if clean_body:
        replacement.extend(["", *clean_body.splitlines()])
    updated = [*lines[:start], *replacement, "", *lines[end:]]
    path.write_text("\n".join(updated).strip() + "\n", encoding="utf-8")
