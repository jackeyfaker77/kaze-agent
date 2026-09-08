"""Idempotent, non-destructive migration of the former character workspace."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".migration-tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def migrate_workspace(workspace: Path) -> None:
    """Keep source files and a database backup; never merge distinct chat histories."""
    marker = workspace / "migrations" / "plain-sessions-v1.json"
    if marker.exists():
        return
    legacy = workspace / "roles"
    database = workspace / "sessions.db"
    backup = workspace / "migrations" / "before-plain-sessions-v1"
    mapping = {}
    if database.exists():
        backup.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as connection:
            if not (backup / "sessions.db").exists():
                with sqlite3.connect(backup / "sessions.db") as target:
                    connection.backup(target)
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone()
            if exists:
                rows = connection.execute("SELECT key, metadata FROM sessions WHERE key LIKE 'role:%'").fetchall()
                for old_key, raw_metadata in rows:
                    digest = hashlib.sha256(old_key.encode()).hexdigest()[:24]
                    key = f"desktop:imported-{digest}"
                    if connection.execute("SELECT 1 FROM sessions WHERE key=?", (key,)).fetchone():
                        raise ValueError(f"会话迁移目标已存在: {key}")
                    metadata = json.loads(raw_metadata or "{}")
                    title = metadata.get("title") or metadata.get("role_name") or "导入的会话"
                    metadata = {k: v for k, v in metadata.items()
                                if not k.startswith(("role_", "current_mood", "relationship"))
                                and k not in {"active_illustration", "thread_id"}}
                    metadata.update(title=title, imported_from=old_key)
                    connection.execute("UPDATE sessions SET key=?, metadata=? WHERE key=?",
                                       (key, json.dumps(metadata, ensure_ascii=False), old_key))
                    connection.execute("UPDATE messages SET session_key=? WHERE session_key=?", (key, old_key))
                    mapping[old_key] = key
    merged = []
    if legacy.exists():
        roles_manifest = legacy / "roles.json"
        if roles_manifest.exists():
            backup.mkdir(parents=True, exist_ok=True)
            if not (backup / "roles.json").exists():
                shutil.copy2(roles_manifest, backup / "roles.json")
            roles = json.loads(roles_manifest.read_text(encoding="utf-8")).get("roles", [])
            if isinstance(roles, dict):
                roles = list(roles.values())
            channels_path = workspace / "channels.json"
            channels = json.loads(channels_path.read_text(encoding="utf-8")) if channels_path.exists() else {"allow_from": {}}
            allowed = channels.setdefault("allow_from", {})
            for role in roles:
                for binding in role.get("channel_bindings", []):
                    channel = str(binding.get("channel") or "")
                    if channel:
                        allowed[channel] = sorted(set(allowed.get(channel, [])) | set(binding.get("allow_from", [])))
            _atomic_text(channels_path, json.dumps(channels, ensure_ascii=False, indent=2))
        for source in sorted(legacy.glob("*/memory/**/*.md")):
            if not source.resolve().is_relative_to(legacy.resolve()):
                continue
            relative = source.relative_to(legacy)
            archive = workspace / "memory" / "imports" / relative
            archive.parent.mkdir(parents=True, exist_ok=True)
            if not archive.exists():
                shutil.copy2(source, archive)
            # Persona SELF and rolling context stay archived, never become global identity.
            if source.name not in {"MEMORY.md", "HISTORY.md", "PENDING.md"}:
                continue
            destination = workspace / "memory" / source.name
            contents = source.read_text(encoding="utf-8")
            identity = hashlib.sha256((relative.as_posix() + contents).encode()).hexdigest()
            stamp = f"<!-- imported-memory:{identity} -->"
            current = destination.read_text(encoding="utf-8") if destination.exists() else ""
            if stamp not in current:
                _atomic_text(destination, current + f"\n\n{stamp}\n## 导入记录：{relative.as_posix()}\n\n" + contents)
            merged.append(relative.as_posix())
        # Preserve complete sprite packages in the application-wide package directory.
        for manifest in sorted(legacy.glob("assets/*/pets/*/pet.json")):
            source = manifest.parent
            if not source.resolve().is_relative_to(legacy.resolve()):
                continue
            digest = hashlib.sha256(source.relative_to(legacy).as_posix().encode()).hexdigest()[:12]
            destination = workspace / "pets" / f"imported-{digest}"
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, destination)
    schedules = workspace / "schedules.json"
    if schedules.exists():
        jobs = json.loads(schedules.read_text(encoding="utf-8"))
        if any("role_id" in job for job in jobs):
            backup.mkdir(parents=True, exist_ok=True)
            if not (backup / "schedules.json").exists():
                shutil.copy2(schedules, backup / "schedules.json")
            for job in jobs:
                old_role = job.pop("role_id", None)
                job.pop("role_config_version", None)
                target = str(job.get("chat_id") or "")
                if job.get("channel") == "desktop":
                    old_key = target if target.startswith("role:") else f"role:{old_role}"
                    key = mapping.get(old_key) or (f"desktop:imported-{hashlib.sha256(old_key.encode()).hexdigest()[:24]}" if old_role else target)
                    job["session_key"] = job["chat_id"] = key
                else:
                    job["session_key"] = f"{job['channel']}:{target}"
            _atomic_text(schedules, json.dumps(jobs, ensure_ascii=False, indent=2))
    _atomic_text(marker, json.dumps({"version": 1, "sessions": mapping, "memory_imports": merged}, ensure_ascii=False, indent=2))
