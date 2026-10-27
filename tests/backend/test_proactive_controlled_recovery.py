"""Production Host/kernel/queue recovery after hard process exits."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('scenario', ['before_model', 'after_model', 'after_decision',
    'after_enqueue', 'physical_unknown', 'model_unknown', 'after_receipt', 'after_queue_done',
    'after_finalization', 'ack_unknown', 'after_ack', 'after_settled', 'canonical_response', 'after_completed',
    'ack_rejected', 'ack_unsuccessful', 'ack_timeout'])
def test_confirmed_recovery_does_not_repeat_committed_work(tmp_path, scenario):
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'PYTHONPATH': str(root / 'apps/backend'), 'PYTHONIOENCODING': 'utf-8'}
    def run(phase):
        return subprocess.run([sys.executable, str(root / 'tests/e2e/proactive_event_crash.py'),
            str(tmp_path), scenario, phase], env=env, cwd=root, capture_output=True,
            text=True, encoding='utf-8', timeout=70)
    crashed = run('crash')
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    reports = []
    for _ in range(2):
        recovered = run('controlled')
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        reports.append(json.loads(recovered.stdout))
    assert reports[0] == reports[1]
    report = reports[0]
    assert report['model'] == 1
    assert report['send'] == (0 if scenario == 'model_unknown' else 1)
    assert report['ack'] <= 1
    assert len(report['messages']) == (0 if scenario in {'physical_unknown', 'model_unknown'} else 1)
    if scenario in {'ack_rejected', 'ack_unsuccessful', 'ack_timeout'}:
        assert report['status'] == 'needs_review'
        assert report['ack'] == report['send'] == report['model'] == 1
        assert 'unknown_effect' in report['recovery']
        assert 'not_interrupted' not in report['recovery']
        assert 'boundary_settled' not in report['recovery']
    elif scenario not in {'physical_unknown', 'model_unknown', 'ack_unknown', 'after_completed'}:
        assert report['recovery'] == 'completed', report
    elif scenario in {'physical_unknown', 'ack_unknown'}:
        assert 'unknown_effect' in report['recovery']
    elif scenario == 'model_unknown':
        assert 'unknown_model' in report['recovery']


def test_crash_after_unique_claim_resumes_linked_execution(tmp_path):
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'PYTHONPATH': str(root / 'apps/backend'), 'PYTHONIOENCODING': 'utf-8'}
    reports = []
    for phase in ['crash', 'claim_crash', 'controlled', 'controlled']:
        result = subprocess.run([sys.executable, str(root / 'tests/e2e/proactive_event_crash.py'),
            str(tmp_path), 'after_model', phase], env=env, cwd=root, capture_output=True,
            text=True, encoding='utf-8', timeout=70)
        assert result.returncode == (73 if phase in {'crash', 'claim_crash'} else 0), result.stdout + result.stderr
        if phase == 'controlled':
            reports.append(json.loads(result.stdout))
    assert reports[0] == reports[1]
    assert reports[0]['model'] == reports[0]['send'] == reports[0]['ack'] == 1
    assert reports[0]['recovery'] == 'completed'
