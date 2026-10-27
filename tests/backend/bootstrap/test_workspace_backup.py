"""Cold snapshot completeness, publication failures and stale-effect isolation."""
from contextlib import closing
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from bootstrap.host_state import WorkspaceInUse, WorkspaceOwner
from bootstrap.workspace_backup import create_backup, restore_backup, verify_backup
from core.common.restore_safety import QUARANTINE, RestoreQuarantined, require_live_workspace
from bus.durable import Outbox, TransportStore
from bus.chat_lane import ChatLane


ROOT = Path(__file__).resolve().parents[3]


def seed(path):
    path.mkdir()
    (path / 'config.toml').write_text('# offline fixture\n', encoding='utf-8')
    (path / 'schedules.json').write_text('{"jobs":[{"id":"pending"}]}')
    (path / 'memory').mkdir()
    (path / 'memory' / 'MEMORY.md').write_text('original user fact', encoding='utf-8')
    with closing(sqlite3.connect(path / 'sessions.db')) as db:
        db.execute('CREATE TABLE messages (id TEXT PRIMARY KEY, content TEXT)')
        db.execute("INSERT INTO messages VALUES ('one','history')")
        db.commit()
    store = TransportStore(path)
    try:
        store.put('out', 'fixture', {'message': 'only once'})
    finally:
        store.close()


def test_cold_backup_restores_all_declared_roots_and_wal(tmp_path):
    source, external = tmp_path / 'source', tmp_path / 'calendar'
    seed(source)
    external.mkdir()
    # A read-only idle connection can keep committed WAL alive after writer exit.
    writer = sqlite3.connect(external / 'agenda.sqlite3')
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('CREATE TABLE todos (id TEXT)')
    writer.execute("INSERT INTO todos VALUES ('todo-1')")
    writer.commit()
    try:
        bundle = tmp_path / 'backup'
        result = create_backup(source, bundle, quiesced=True, data_roots={'calendar': external}, revision='fixture')
        assert result['databases'] == 3
        manifest = verify_backup(bundle)
        assert not any(e['path'].endswith(('-wal', '-shm')) for e in manifest['files'])
        restored = restore_backup(bundle, tmp_path / 'restored')
        assert restored['state'] == 'isolated'
        with closing(sqlite3.connect(tmp_path / 'restored/calendar/agenda.sqlite3')) as db:
            assert db.execute('SELECT * FROM todos').fetchall() == [('todo-1',)]
        assert (tmp_path / 'restored/workspace/memory/MEMORY.md').read_text() == 'original user fact'
        with pytest.raises(RestoreQuarantined):
            require_live_workspace(Path(restored['workspace']))
        require_live_workspace(source)
    finally:
        writer.close()


def test_live_workspace_and_unconfirmed_external_writers_are_refused(tmp_path):
    source = tmp_path / 'source'
    seed(source)
    with pytest.raises(ValueError, match='quiesced'):
        create_backup(source, tmp_path / 'backup', quiesced=False)
    owner = WorkspaceOwner(source, 'host')
    try:
        with pytest.raises(WorkspaceInUse):
            create_backup(source, tmp_path / 'backup', quiesced=True)
        assert not (tmp_path / 'backup').exists()
    finally:
        owner.close()


@pytest.mark.parametrize('corruption', ['content', 'extra', 'marker', 'traversal', 'version', 'duplicate'])
def test_corrupt_or_unsafe_bundle_is_never_restored(tmp_path, corruption):
    source, bundle = tmp_path / 'source', tmp_path / 'backup'
    seed(source)
    create_backup(source, bundle, quiesced=True)
    manifest = json.loads((bundle / 'manifest.json').read_text())
    if corruption == 'content':
        (bundle / 'data/workspace/memory/MEMORY.md').write_text('changed')
    elif corruption == 'extra':
        (bundle / 'data/workspace/unlisted').write_text('extra')
    elif corruption == 'marker':
        (bundle / 'data/workspace' / QUARANTINE).unlink()
    else:
        if corruption == 'traversal':
            manifest['files'][0]['path'] = '../outside'
        elif corruption == 'version':
            manifest['version'] = 999
        else:
            manifest['files'].append(manifest['files'][0])
        (bundle / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises((ValueError, OSError)):
        restore_backup(bundle, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


def test_paths_never_overwrite_or_recurse_into_source(tmp_path):
    source = tmp_path / 'source'
    seed(source)
    for dest in (source, source / 'nested'):
        with pytest.raises(ValueError):
            create_backup(source, dest, quiesced=True)
    create_backup(source, tmp_path / 'backup', quiesced=True)
    with pytest.raises(ValueError):
        restore_backup(tmp_path / 'backup', source)
    assert (source / 'memory/MEMORY.md').read_text() == 'original user fact'


def test_source_mutation_fails_publication(tmp_path, monkeypatch):
    import bootstrap.workspace_backup as module
    source = tmp_path / 'source'
    seed(source)
    copy = module._copy_file
    def racing_copy(src, target):
        result = copy(src, target)
        if src.name == 'MEMORY.md':
            src.write_text('concurrent writer must be stopped')
        return result
    monkeypatch.setattr(module, '_copy_file', racing_copy)
    with pytest.raises(ValueError, match='源文件变化'):
        create_backup(source, tmp_path / 'backup', quiesced=True)
    assert not (tmp_path / 'backup').exists()


def test_damaged_database_header_is_not_accepted_as_an_ordinary_file(tmp_path):
    source = tmp_path / 'source'
    seed(source)
    (source / 'sessions.db').write_bytes(b'damaged database')
    with pytest.raises(ValueError, match='数据库头'):
        create_backup(source, tmp_path / 'backup', quiesced=True)
    assert not (tmp_path / 'backup').exists()


def test_transport_and_upload_attachments_preserve_opaque_bytes_and_hash_names(tmp_path):
    source = tmp_path / 'source'
    seed(source)
    uploads = source / 'uploads'
    uploads.mkdir()
    database = uploads / 'document.db'
    with closing(sqlite3.connect(database)) as db:
        db.execute('PRAGMA auto_vacuum=FULL')
        db.execute('CREATE TABLE original(value TEXT)')
        db.execute("INSERT INTO original VALUES ('unchanged attachment bytes')")
        db.commit()
    for name in ('opaque.db', 'document.db-wal', 'document.db-shm', 'opaque.lock'):
        (uploads / name).write_bytes(b'opaque user document: ' + name.encode())
    store = TransportStore(source)
    expected = {}
    try:
        for path in uploads.iterdir():
            content = path.read_bytes()
            expected[path.relative_to(source).as_posix()] = content
            snapshot = Path(store.snapshot_file(str(path)))
            assert snapshot.name == hashlib.sha256(content).hexdigest() + path.suffix
            expected[snapshot.relative_to(source).as_posix()] = content
    finally:
        store.close()
    create_backup(source, tmp_path/'backup', quiesced=True)
    manifest = verify_backup(tmp_path/'backup')
    attachment_entries = [entry for entry in manifest['files']
                          if entry['path'].removeprefix('data/workspace/') in expected]
    assert len(attachment_entries) == len(expected)
    assert all(not entry['sqlite'] for entry in attachment_entries)
    restored = restore_backup(tmp_path/'backup', tmp_path/'restored')
    for relative, content in expected.items():
        assert (Path(restored['workspace'])/relative).read_bytes() == content
        assert (tmp_path/'backup/data/workspace'/relative).read_bytes() == content


@pytest.mark.asyncio
async def test_old_snapshot_cannot_resend_effect_completed_after_backup(tmp_path):
    from bootstrap.app import AppRuntime
    from bootstrap.runtime_host import RuntimeHost
    from agent.config_models import Config
    source, bundle = tmp_path / 'source', tmp_path / 'backup'
    seed(source)
    create_backup(source, bundle, quiesced=True)
    # The external service observes a successful delivery AFTER the snapshot.
    store = TransportStore(source)
    box = Outbox(store, ChatLane())
    wire = []
    async def physical():
        wire.append('sent')
        return {'message_id': 'receipt-after-backup'}
    async def handler(payload):
        return await box.part('0:text', payload, physical)
    box.handlers['fixture'] = handler
    try:
        await box.deliver('out')
        assert store.get('out')['state'] == 'done'
    finally:
        store.close()
    for attempt in range(2):
        restored = restore_backup(bundle, tmp_path / f'restored-{attempt}')
        workspace = Path(restored['workspace'])
        with closing(sqlite3.connect(workspace / 'private_runtime/transport/queue.sqlite3')) as db:
            assert db.execute("SELECT state FROM jobs WHERE id='out'").fetchone() == ('pending',)
        # Both local bridge and standalone/remote Host use this production fence.
        for mode in ('bridge', 'host'):
            runtime = AppRuntime(Config(provider='openai', model='offline', api_key='offline'), workspace)
            with pytest.raises(RestoreQuarantined):
                await RuntimeHost(runtime, mode=mode).run()
            assert runtime.core is None and not runtime.background_tasks
        assert wire == ['sent']
        assert not (workspace / 'private_runtime/host/owner.lock').exists()


@pytest.mark.parametrize('mode', ['host', 'bridge', 'desktop', 'remote-host'])
def test_production_cli_refuses_restored_workspace_before_config_loading(tmp_path, mode):
    source = tmp_path / 'source'
    seed(source)
    create_backup(source, tmp_path / 'backup', quiesced=True)
    result = restore_backup(tmp_path / 'backup', tmp_path / 'restored')
    config = Path(result['workspace']) / 'config.toml'
    config.write_text('invalid = [toml')  # Proves config/provider setup was not reached.
    args = [sys.executable, str(ROOT / 'apps/backend/main.py'), 'host' if mode == 'remote-host' else mode,
            '--workspace', result['workspace'], '--config', str(config)]
    if mode == 'remote-host':
        args += ['--remote-port', '8765', '--remote-token-file', str(tmp_path/'unread-token')]
    ran = subprocess.run(args, cwd=ROOT,
        capture_output=True, text=True, encoding='utf-8', timeout=30,
        env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    assert ran.returncode == 3, ran.stdout + ran.stderr
    assert '隔离' in ran.stderr


@pytest.mark.parametrize('operation', ['backup', 'restore', 'publish'])
def test_real_process_crash_never_publishes_unverified_copy(tmp_path, operation):
    source = tmp_path / 'source'
    seed(source)
    if operation == 'restore':
        create_backup(source, tmp_path / 'backup', quiesced=True)
    program = '''
import os, sys
from pathlib import Path
import bootstrap.workspace_backup as b
root=Path(sys.argv[1]); operation=sys.argv[2]
if operation == 'backup':
    copy=b._copy_file
    def fault(*args):
        copy(*args)
        os._exit(73)
    b._copy_file=fault
    b.create_backup(root/'source',root/'backup',quiesced=True)
elif operation == 'publish':
    def crash_publish(*args):
        os._exit(73)
    Path.rename=crash_publish
    b.create_backup(root/'source',root/'backup',quiesced=True)
else:
    copy=b.shutil.copyfile
    def fault(src,dst):
        result=copy(src,dst)
        if Path(dst).name != b.QUARANTINE:
            os._exit(73)
        return result
    b.shutil.copyfile=fault
    b.restore_backup(root/'backup',root/'restored')
'''
    ran = subprocess.run([sys.executable, '-c', program, str(tmp_path), operation], cwd=ROOT,
        capture_output=True, timeout=30, env={**os.environ, 'PYTHONPATH': str(ROOT / 'apps/backend')})
    assert ran.returncode == 73, ran.stderr
    name = 'restored' if operation == 'restore' else 'backup'
    assert not (tmp_path / name).exists()
    partial = next(tmp_path.glob('.' + name + '.partial-*'))
    with pytest.raises(RestoreQuarantined):
        require_live_workspace(partial)
    if operation == 'publish':
        with pytest.raises(ValueError, match='partial'):
            verify_backup(partial)
    # Failed attempts do not hold the Host lease or prevent a fresh valid backup.
    create_backup(source, tmp_path / 'retry-backup', quiesced=True)


def test_missing_or_malformed_marker_cannot_silently_enable_nested_restore(tmp_path):
    (tmp_path / QUARANTINE).write_text('not json')
    nested = tmp_path / 'workspace'
    nested.mkdir()
    with pytest.raises(RestoreQuarantined):
        require_live_workspace(nested)


def test_cli_backup_verify_restore_do_not_load_runtime_config(tmp_path, monkeypatch, capsys):
    import main
    source = tmp_path / 'source'
    seed(source)
    def forbidden(*args):
        raise AssertionError('maintenance must not load runtime config')
    monkeypatch.setattr(main.Config, 'load', forbidden)
    assert main.main(['workspace-backup', '--workspace', str(source), '--destination', str(tmp_path/'backup'),
                      '--quiesced', '--revision', 'tested']) == 0
    assert main.main(['backup-verify', '--backup', str(tmp_path/'backup')]) == 0
    assert main.main(['workspace-restore', '--backup', str(tmp_path/'backup'),
                      '--destination', str(tmp_path/'restored')]) == 0
    assert 'isolated' in capsys.readouterr().out


@pytest.mark.parametrize('command', ['host', 'bridge'])
def test_restored_config_cannot_start_an_unmarked_workspace(tmp_path, monkeypatch, command):
    import main
    isolated = tmp_path / 'isolated'
    isolated.mkdir()
    (isolated / QUARANTINE).write_text('{}')
    config = isolated / 'config.toml'
    config.write_text('not valid toml')
    def forbidden(*args):
        raise AssertionError('restored config must not be loaded')
    monkeypatch.setattr(main.Config, 'load', forbidden)
    assert main.main([command, '--workspace', str(tmp_path / 'new'), '--config', str(config)]) == 3
    assert not (tmp_path / 'new').exists()


def test_source_symlink_is_not_followed(tmp_path):
    source = tmp_path / 'source'
    seed(source)
    secret = tmp_path / 'outside'
    secret.write_text('outside root')
    try:
        (source / 'link').symlink_to(secret)
    except OSError:
        pytest.skip('OS does not grant symlink creation')
    with pytest.raises(ValueError, match='链接'):
        create_backup(source, tmp_path/'backup', quiesced=True)
