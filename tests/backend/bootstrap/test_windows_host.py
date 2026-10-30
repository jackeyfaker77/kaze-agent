"""The no-console entry uses the real Host and persistent stores, not a child supervisor."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from bootstrap.host_state import host_status, request_host_stop
from windows_host import LogStream

ROOT = Path(__file__).resolve().parents[3]


def test_consoleless_log_stream_rotates_and_preserves_unicode(tmp_path):
    stream = LogStream(tmp_path / 'host.log')
    stream.handler.maxBytes = 80
    try:
        for i in range(10):
            stream.write(f'{i}:后台启动与恢复正常\n')
        stream.flush()
        assert '9:后台启动与恢复正常' in (tmp_path / 'host.log').read_text(encoding='utf-8')
        assert len(list(tmp_path.glob('host.log*'))) <= 4
        assert (tmp_path / 'host.log.1').exists()
    finally:
        stream.handler.close()


@pytest.mark.parametrize('legacy_conflict_pause', [False, True])
def test_windows_entry_recovers_after_lock_conflict_and_stops_cleanly(tmp_path, legacy_conflict_pause):
    config = tmp_path / 'config.toml'
    config.write_text('''[[llm.registrations]]
id = "00000000-0000-4000-a000-000000000001"
provider = "openai"
model = "offline"
api_key = "offline"
[proactive]
enabled = false
[agent.maintenance]
memory_optimizer_enabled = false
''')
    command = [sys.executable, str(ROOT/'apps/backend/windows_host.py'), 'host', '--workspace', str(tmp_path), '--config', str(config)]
    from core.execution.lease import EpochLease
    lock = tmp_path/'private_runtime/host/owner.lock'
    lock.parent.mkdir(parents=True)
    pause = tmp_path/'private_runtime/windows-host/paused.json'
    lease = EpochLease(lock)
    try:
        conflict = subprocess.run(command, capture_output=True, timeout=20)
        assert conflict.returncode == 0
        assert not pause.exists()  # A transient probe/owner cannot disable recovery.
    finally:
        lease.close()
    if legacy_conflict_pause:
        pause.write_text(json.dumps({'exit_code': 2, 'reason': 'legacy conflict'}))
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               env={**os.environ, 'PYTHONIOENCODING':'utf-8'})
    try:
        deadline = time.monotonic()+30
        while time.monotonic()<deadline:
            assert process.poll() is None, process.communicate()[1]
            status_file = tmp_path/'private_runtime/host/status.json'
            try:
                state = json.loads(status_file.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                state = {}
            if state.get('healthy'):
                state = host_status(tmp_path)
                if state.get('healthy'): break
            time.sleep(.1)
        else: raise AssertionError('Host did not become healthy')
        # Windows venv launchers add a shim PID; Host ownership records the interpreter PID.
        assert isinstance(state['pid'], int)
        assert state['mode'] == 'host'
        conflict=subprocess.run(command, capture_output=True, timeout=20)
        assert conflict.returncode == 0
        assert not pause.exists() or json.loads(pause.read_text())['exit_code'] == 2
        assert host_status(tmp_path)['owner_id'] == state['owner_id']
        assert request_host_stop(tmp_path)['requested']
        assert process.wait(timeout=20) == 0
        assert not host_status(tmp_path)['running']
        assert json.loads(pause.read_text())['exit_code'] == 0
        assert 'exit=2' in (tmp_path/'private_runtime/windows-host/host.log').read_text(encoding='utf-8')
        before_log=(tmp_path/'private_runtime/windows-host/host.log').read_bytes()
        assert subprocess.run(command,capture_output=True,timeout=10).returncode==0
        assert not host_status(tmp_path)['running']
        assert (tmp_path/'private_runtime/windows-host/host.log').read_bytes()==before_log
    finally:
        request_host_stop(tmp_path)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_windows_task_plan_quotes_paths_and_limits_autostart_to_current_user(tmp_path):
    if sys.platform != 'win32':
        import pytest
        pytest.skip('Windows Task Scheduler contract')
    import xml.etree.ElementTree as ET
    workspace=tmp_path/'含 空格 & symbols'
    completed=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'deploy/windows-runtime.ps1'),
                              '-Action','Plan','-Workspace',str(workspace)],capture_output=True,timeout=20)
    assert completed.returncode==0, completed.stderr
    plan=json.loads(completed.stdout.decode('utf-8-sig'))
    task=ET.fromstring(plan['xml']); ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    def value(path):
        element=task.find(path,ns)
        assert element is not None and element.text is not None
        return element.text
    assert value('t:Principals/t:Principal/t:LogonType')=='InteractiveToken'
    assert value('t:Principals/t:Principal/t:RunLevel')=='LeastPrivilege'
    assert value('t:Settings/t:MultipleInstancesPolicy')=='IgnoreNew'
    assert value('t:Triggers/t:TimeTrigger/t:Repetition/t:Interval')=='PT1M'
    assert value('t:Settings/t:Enabled')=='false'
    assert value('t:Settings/t:ExecutionTimeLimit')=='PT0S'
    assert str(workspace) in value('t:Actions/t:Exec/t:Arguments')
    assert '"--remote-token-file"' in value('t:Actions/t:Exec/t:Arguments')
    assert not workspace.exists()  # Planning cannot create credentials or configuration.


@pytest.mark.parametrize('scenario', ['startup', 'owner_change', 'queued', 'timeout',
                                    'disabled_running_start', 'enabled_running_start'])
def test_management_lifecycle_transitions(tmp_path, scenario):
    if sys.platform != 'win32':
        pytest.skip('PowerShell management lifecycle')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', str(ROOT/'tests/e2e/windows_runtime_lifecycle.ps1'),
        '-Scenario', scenario, '-TestDirectory', str(tmp_path)], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace')


def test_windows_entry_refuses_quarantined_restore_without_writing_logs(tmp_path):
    (tmp_path/'.kaze-restore-quarantine.json').write_text('{}')
    result=subprocess.run([sys.executable,str(ROOT/'apps/backend/windows_host.py'),'host','--workspace',str(tmp_path)],capture_output=True,timeout=15)
    assert result.returncode == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ['.kaze-restore-quarantine.json']
