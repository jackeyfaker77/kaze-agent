"""Cold, explicit-coverage backups and fail-closed isolated restore.

The Host lock fences cooperating Kaze writers. Operators must also stop external
writers; per-database SQLite snapshots are NOT a distributed transaction.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
from uuid import uuid4

from bootstrap.host_state import WorkspaceOwner, _write
from core.common.restore_safety import QUARANTINE

VERSION = 1
EXCLUDED_DIRS = {'.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache'}
WORKSPACE_EXCLUDED = {'.desktop', '.tmp', 'pets'}
HOST_TRANSIENT = {'private_runtime/host/status.json', 'private_runtime/host/stop.json'}
OPAQUE_PREFIXES = ('uploads/', 'private_runtime/transport/attachments/')


def _opaque(relative: str, workspace: bool) -> bool:
    return workspace and relative.startswith(OPAQUE_PREFIXES)


def _hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _sqlite(path: Path) -> bool:
    with path.open('rb') as stream:
        return stream.read(16) == b'SQLite format 3\x00'


def _safe_relative(value: str) -> Path:
    parsed = PurePosixPath(value)
    if (not value or parsed.is_absolute() or any(p in {'..', '.'} for p in value.split('/'))
            or '\\' in value or ':' in value or '' in value.split('/')):
        raise ValueError('备份清单包含不安全路径')
    return Path(*parsed.parts)


def _walk(root: Path, *, workspace: bool = False):
    files, excluded = {}, []
    def visit(directory):
        for item in sorted(directory.iterdir()):
            rel = item.relative_to(root).as_posix()
            if (item.name in EXCLUDED_DIRS or (workspace and rel in WORKSPACE_EXCLUDED)
                    or (workspace and rel in HOST_TRANSIENT)
                    or (workspace and item.name.endswith('.lock') and
                        (item.parent.relative_to(root).as_posix() == 'private_runtime/execution'
                         or rel in {'private_runtime/host/owner.lock', 'private_runtime/transport/owner.lock'}))):
                excluded.append(rel)
                continue
            if item.is_symlink() or item.is_junction():
                raise ValueError(f'备份不跟随符号链接或 junction: {rel}')
            if item.is_dir():
                visit(item)
            elif item.is_file():
                files[rel] = item
            else:
                raise ValueError(f'备份不支持特殊文件: {rel}')
    visit(root)
    return files, excluded


def _fingerprints(files, *, workspace: bool = False):
    return {key: (path.stat().st_size, path.stat().st_mtime_ns) for key, path in files.items()
            if _opaque(key, workspace) or (not key.endswith('-shm')
                and not (key.endswith('-wal') and path.stat().st_size == 0))}


def _integrity(path: Path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError(f'SQLite 完整性检查失败: {path.name}')


def _copy_file(source: Path, target: Path, *, opaque: bool = False):
    target.parent.mkdir(parents=True, exist_ok=True)
    sqlite = not opaque and _sqlite(source)
    if not opaque and source.suffix.lower() in {'.db', '.sqlite', '.sqlite3'} and not sqlite:
        raise ValueError(f'数据库头损坏或格式不支持: {source.name}')
    if sqlite:
        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
            with closing(sqlite3.connect(target)) as dst:
                src.backup(dst)
                dst.execute('PRAGMA journal_mode=DELETE')
        _integrity(target)
    else:
        shutil.copyfile(source, target)
    with target.open('r+b') as stream:
        os.fsync(stream.fileno())
    return sqlite


def _destination(path: Path, forbidden: list[Path]) -> Path:
    target = path.absolute()
    if target.exists() or target.is_symlink():
        raise ValueError('目标必须是尚不存在的新目录，禁止覆盖')
    if not target.parent.is_dir():
        raise ValueError('目标父目录必须存在')
    target = target.resolve()
    if any(target.is_relative_to(root) or root.is_relative_to(target) for root in forbidden):
        raise ValueError('备份与恢复目标不能和源目录重叠')
    return target


def create_backup(workspace: Path, destination: Path, *, quiesced: bool,
                  data_roots: dict[str, Path] | None = None, revision: str = '') -> dict:
    if not quiesced:
        raise ValueError('必须先停止 Host 及所有独立 MCP/领域写入者，并提供 --quiesced')
    workspace = workspace.resolve(strict=True)
    roots = {'workspace': workspace}
    for name, path in (data_roots or {}).items():
        if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,63}', name) or name == 'workspace':
            raise ValueError('外部数据根名称无效')
        root = path.resolve(strict=True)
        if not root.is_dir() or any(root.is_relative_to(r) or r.is_relative_to(root) for r in roots.values()):
            raise ValueError('数据根必须是不重叠的目录')
        roots[name] = root
    target = _destination(destination, list(roots.values()))
    owner = WorkspaceOwner(workspace, 'maintenance')
    try:
        backup_id = uuid4().hex
        stage = target.with_name('.' + target.name + '.partial-' + backup_id)
        stage.mkdir(mode=0o700)
        marker = {'version': VERSION, 'backup_id': backup_id, 'reason': 'historical_external_effects_unknown'}
        _write(stage / QUARANTINE, marker)
        entries, inventories, exclusions = [], {}, {}
        for name, root in roots.items():
            files, excluded = _walk(root, workspace=name == 'workspace')
            inventories[name] = _fingerprints(files, workspace=name == 'workspace')
            exclusions[name] = excluded
            output = stage / 'data' / name
            output.mkdir(parents=True)
            _write(output / QUARANTINE, marker)
            for rel, source in files.items():
                if rel == QUARANTINE:
                    continue
                opaque = _opaque(rel, name == 'workspace')
                if not opaque and rel.endswith(('-wal', '-shm')):
                    database = root / rel.rsplit('-', 1)[0]
                    if not database.is_file() or not _sqlite(database):
                        raise ValueError(f'无法确认 SQLite 旁文件所属数据库: {rel}')
                    continue  # SQLite backup includes committed WAL contents.
                copied = output / rel
                sqlite = _copy_file(source, copied, opaque=True) if opaque else _copy_file(source, copied)
                entries.append({'path': copied.relative_to(stage).as_posix(),
                                'sha256': _hash(copied), 'size': copied.stat().st_size, 'sqlite': sqlite})
            marker_path = output / QUARANTINE
            entries.append({'path': marker_path.relative_to(stage).as_posix(), 'sha256': _hash(marker_path),
                            'size': marker_path.stat().st_size, 'sqlite': False})
        for name, root in roots.items():
            files, _ = _walk(root, workspace=name == 'workspace')
            if inventories[name] != _fingerprints(files, workspace=name == 'workspace'):
                raise ValueError('备份期间源文件变化；拒绝发布非静止快照')
        manifest = {'version': VERSION, 'backup_id': backup_id, 'state': 'complete',
                    'created_at': datetime.now(timezone.utc).isoformat(), 'revision': revision,
                    'consistency': 'offline-host-lock-and-operator-quiesced-external-writers',
                    'roots': {k: str(v) for k, v in roots.items()}, 'excluded': exclusions,
                    'files': entries, 'quarantine_sha256': _hash(stage / QUARANTINE)}
        _write(stage / 'manifest.json', manifest)
        verify_backup(stage, _allow_partial=True)
        if target.exists():
            raise ValueError('目标已存在，拒绝发布')
        stage.rename(target)
        return {'backup': str(target), 'backup_id': backup_id, 'files': len(entries),
                'databases': sum(e['sqlite'] for e in entries), 'isolated': True}
    finally:
        owner.close()


def verify_backup(backup: Path, *, _allow_partial: bool = False) -> dict:
    backup = backup.resolve(strict=True)
    if not _allow_partial and backup.name.startswith('.') and '.partial-' in backup.name:
        raise ValueError('备份尚未原子发布，不能使用 partial 目录')
    # No exclusions on bundles: extra files could otherwise escape verification.
    def physical_files(directory):
        result = set()
        for entry in directory.iterdir():
            if entry.is_symlink() or entry.is_junction():
                raise ValueError('备份内禁止链接')
            if entry.is_dir():
                result.update(physical_files(entry))
            elif entry.is_file():
                result.add(entry.relative_to(backup).as_posix())
            else:
                raise ValueError('备份包含特殊文件')
        return result
    actual = physical_files(backup)
    manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version') != VERSION or manifest.get('state') != 'complete':
        raise ValueError('不支持或未完成的备份')
    expected = {'manifest.json', QUARANTINE}
    if _hash(backup / QUARANTINE) != manifest['quarantine_sha256']:
        raise ValueError('隔离标记损坏')
    roots = manifest['roots']
    if 'workspace' not in roots:
        raise ValueError('备份缺少工作区')
    for name in roots:
        if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,63}', name):
            raise ValueError('无效数据根')
    for entry in manifest['files']:
        path = _safe_relative(entry['path'])
        if len(path.parts) < 3 or path.parts[0] != 'data' or path.parts[1] not in roots or entry['path'] in expected:
            raise ValueError('清单路径重复或不属于数据根')
        expected.add(entry['path'])
        target = backup / path
        if target.stat().st_size != entry['size'] or _hash(target) != entry['sha256']:
            raise ValueError(f'备份文件校验失败: {entry["path"]}')
        if entry['sqlite']:
            _integrity(target)
    if actual != expected or any(f'data/{name}/{QUARANTINE}' not in expected for name in roots):
        raise ValueError('备份文件清单不完整或存在额外文件')
    return manifest


def restore_backup(backup: Path, destination: Path) -> dict:
    backup = backup.resolve(strict=True)
    manifest = verify_backup(backup)
    target = _destination(destination, [backup, *[Path(p).resolve() for p in manifest['roots'].values()]])
    stage = target.with_name('.' + target.name + '.partial-' + uuid4().hex)
    stage.mkdir(mode=0o700)
    _write(stage / QUARANTINE, {'version': VERSION, 'backup_id': manifest['backup_id'], 'state': 'restoring'})
    # Mark each root BEFORE copying, so even a crash leaves inert partial data.
    for name in manifest['roots']:
        (stage / name).mkdir()
        shutil.copyfile(backup / 'data' / name / QUARANTINE, stage / name / QUARANTINE)
    for entry in manifest['files']:
        relative = _safe_relative(entry['path'])
        source = backup / relative
        output = stage.joinpath(*relative.parts[1:])
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, output)
        if _hash(output) != entry['sha256']:
            raise ValueError('恢复期间备份文件变化')
        with output.open('r+b') as stream:
            os.fsync(stream.fileno())
    verify_backup(backup)
    _write(stage / 'restore-report.json', {'version': VERSION, 'backup_id': manifest['backup_id'],
        'state': 'isolated', 'source_manifest_sha256': _hash(backup / 'manifest.json'),
        'revision': manifest['revision'], 'files': len(manifest['files']),
        'warning': '配置中的绝对路径未重写；禁止连接原 MCP、Telegram 或模型。无在线解除隔离命令。'})
    if target.exists():
        raise ValueError('目标已存在')
    stage.rename(target)
    return {'restored': str(target), 'workspace': str(target / 'workspace'),
            'backup_id': manifest['backup_id'], 'state': 'isolated'}
