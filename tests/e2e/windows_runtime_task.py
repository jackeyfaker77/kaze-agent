"""Explicit Windows deployment drill; registers and removes a temporary user task.

Run manually with permission to use Task Scheduler. No real model, Telegram,
user configuration or production workspace is used.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from websockets.sync.client import connect

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'apps/backend'))
from bootstrap.host_state import host_status


def run():
    assert sys.platform=='win32'
    workspace=Path(tempfile.mkdtemp(prefix='windows-task-',dir=ROOT/'.tmp'))
    (workspace/'config.toml').write_text('''[[llm.registrations]]
id = "00000000-0000-4000-a000-000000000001"
provider = "openai"
model = "offline"
api_key = "offline"
[proactive]
enabled = false
[agent.maintenance]
memory_optimizer_enabled = false
''')
    import socket
    with socket.socket() as s:
        s.bind(('127.0.0.1',0)); port=s.getsockname()[1]
    def command(action):
        # pwsh normally cleans this when launching Windows PowerShell; Python does not.
        env={k:v for k,v in os.environ.items() if k.lower()!='psmodulepath'}
        completed=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'deploy/windows-runtime.ps1'),
            '-Action',action,'-Workspace',str(workspace),'-Port',str(port)],capture_output=True,timeout=65,env=env)
        assert completed.returncode==0, completed.stderr.decode(errors='replace')
        return completed.stdout.decode('utf-8-sig',errors='replace')
    report={'workspace':str(workspace),'task_restart':False,'history_preserved':False,'single_owner':False,'clean_stop':False,'conflict_does_not_pause':False,'startup_stop':False}
    def wait_health(previous=None):
        deadline=time.monotonic()+110
        while time.monotonic()<deadline:
            # Wait for a new ready publication before probing the ownership lock.
            # An eager lock probe can itself beat a starting Host to owner.lock.
            try:
                state=json.loads((workspace/'private_runtime/host/status.json').read_text())
            except (FileNotFoundError,json.JSONDecodeError):
                state={}
            if state.get('healthy') and state.get('state')=='running' and state.get('epoch')!=previous:
                checked=host_status(workspace)
                if checked.get('healthy') and checked.get('epoch')==state['epoch']:
                    return checked
            time.sleep(.3)
        raise AssertionError(f'Host not ready: {workspace}')
    def rpc(method,payload):
        token=(workspace/'private_runtime/windows-host/runtime-token').read_text().strip()
        with connect(f'ws://127.0.0.1:{port}',proxy=None,open_timeout=5) as ws:
            ws.send(json.dumps({'type':'auth','token':token}))
            assert json.loads(ws.recv(timeout=5))['type']=='auth.ok'
            ws.send(json.dumps({'id':'task-test','method':method,'payload':payload}))
            while True:
                value=json.loads(ws.recv(timeout=10))
                if value.get('type')=='response':
                    assert value['error'] is None
                    return value['payload']
    installed=False
    try:
        command('Install'); installed=True
        print('Installed isolated task',flush=True)
        command('Start'); before=wait_health()
        initial_task=json.loads(command('Status'))
        print(json.dumps({'task_before_crash':initial_task['state'],'result':initial_task['lastResult']}),flush=True)
        assert initial_task['state']=='Running'
        rpc('session.create',{'session_key':'desktop:task-drill','title':'survives worker crash'})
        command('Start')
        assert host_status(workspace)['owner_id']==before['owner_id']
        report['single_owner']=True
        conflict=subprocess.run([sys.executable,str(ROOT/'apps/backend/windows_host.py'),
            'host','--workspace',str(workspace),'--config',str(workspace/'config.toml')],
            capture_output=True,timeout=30)
        assert conflict.returncode==0
        assert not (workspace/'private_runtime/windows-host/paused.json').exists()
        assert host_status(workspace)['owner_id']==before['owner_id']
        report['conflict_does_not_pause']=True
        # Kill only the verified isolated Host, never a production process.
        assert host_status(workspace)['pid']==before['pid'] and workspace.name.startswith('windows-task-')
        subprocess.run(['taskkill','/PID',str(before['pid']),'/F'],check=True,capture_output=True)
        print('Killed isolated Host; waiting for Task Scheduler restart (1 minute)',flush=True)
        after=wait_health(before['epoch'])
        assert after['root_id']==before['root_id'] and after['pid']!=before['pid']
        report['task_restart']=True
        assert rpc('session.get',{'session_key':'desktop:task-drill'})['title']=='survives worker crash'
        report['history_preserved']=True
        command('Stop')
        assert not host_status(workspace)['running']
        time.sleep(2)
        assert not host_status(workspace)['running']
        report['clean_stop']=True
        # Stop immediately after requesting a start, without waiting for health.
        # The deterministic lifecycle fixture also covers the pre-owner window.
        command('Start')
        command('Stop')
        assert not host_status(workspace)['running']
        report['startup_stop']=True
    finally:
        if installed: command('Uninstall')
        (workspace/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report),flush=True)


if __name__=='__main__':
    run()
