"""Journal maintenance cannot interfere with an online workspace owner."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bootstrap.host_state import WorkspaceOwner, WorkspaceInUse, journal_maintenance
from core.execution.journal import ExecutionJournal


def test_inherited_owner_identity_does_not_grant_maintenance(tmp_path):
    owner = WorkspaceOwner(tmp_path, 'host')
    try:
        owner.base['pid'] = -1
        with pytest.raises(WorkspaceInUse):
            ExecutionJournal(tmp_path)
        assert not (tmp_path / 'private_runtime/execution/events.sqlite3').exists()
    finally:
        owner.close()


def test_external_open_and_rebuild_rejected_without_touching_host(tmp_path):
    script = '''
import sys
from pathlib import Path
from core.execution.journal import ExecutionJournal
from bootstrap.host_state import WorkspaceInUse
w=Path(sys.argv[1])
peer=ExecutionJournal(w)
print('ready',flush=True)
input()
for operation in [lambda: ExecutionJournal(w), peer.rebuild_projection]:
    try: operation()
    except WorkspaceInUse: pass
    else: raise AssertionError('online maintenance allowed')
peer.close()
'''
    env = {**os.environ, 'PYTHONPATH': str(Path('apps/backend').resolve())}
    child = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env)
    try:
        assert child.stdout.readline().strip() == 'ready'
        owner = WorkspaceOwner(tmp_path, 'host')
        try:
            host = ExecutionJournal(tmp_path)
            try:
                row, _ = host.admit('desktop:test', 'desktop', 'one', {})
                before = host.get(row['turn_id'])
                stdout, stderr = child.communicate('\n', timeout=20)
                assert child.returncode == 0, (stdout, stderr)
                assert host.get(row['turn_id']) == before
                host.start(row['turn_id'])
                host.finish(row['turn_id'], 'completed')
            finally:
                host.close()
        finally:
            owner.close()
        with journal_maintenance(tmp_path):
            with pytest.raises(WorkspaceInUse):
                WorkspaceOwner(tmp_path, 'host')
        offline = ExecutionJournal(tmp_path)
        offline.rebuild_projection()
        offline.close()
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()
