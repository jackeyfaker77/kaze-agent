"""Actual crashes around model/decision/queue/physical-receipt boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('scenario', ['before_model', 'after_model', 'after_decision',
    'after_enqueue', 'physical_unknown', 'after_receipt', 'after_queue_done', 'after_completed'])
def test_crash_recovery_never_replays_model_or_confirmed_send(tmp_path, scenario):
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'PYTHONPATH': str(root / 'apps/backend'), 'PYTHONIOENCODING': 'utf-8'}
    def run(phase):
        return subprocess.run([sys.executable, str(root / 'tests/e2e/proactive_event_crash.py'),
            str(tmp_path), scenario, phase], env=env, cwd=root, capture_output=True,
            text=True, encoding='utf-8', timeout=60)
    crashed = run('crash')
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    reports = []
    for _ in range(2):
        recovered = run('recover')
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        reports.append(json.loads(recovered.stdout))
    assert reports[0] == reports[1]
    report = reports[0]
    assert report['replay_calls'] == 0
    assert report['model'] == (0 if scenario == 'before_model' else 1)
    queued = scenario in {'after_enqueue', 'physical_unknown', 'after_receipt', 'after_queue_done', 'after_completed'}
    assert report['send'] == int(queued)
    saved = queued and scenario != 'physical_unknown'
    assert len(report['messages']) == int(saved)
    if queued:
        assert report['links']
        assert len(report['jobs']) == 1
        assert report['jobs'][0]['state'] == ('needs_review' if scenario == 'physical_unknown' else 'done')
    assert report['status'] == ('completed' if scenario == 'after_completed' else
        'needs_review' if scenario in {'physical_unknown', 'after_receipt'} else 'interrupted')
