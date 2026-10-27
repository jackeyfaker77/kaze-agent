import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('scenario,counts,status', [
    ('before_request_commit', (0, 0, 0), 'interrupted'),
    ('before_network', (0, 0, 0), 'interrupted'),
    ('during_model', (1, 0, 0), 'interrupted'),
    ('after_response', (1, 0, 0), 'interrupted'),
    ('unknown_tool', (1, 1, 0), 'needs_review'),
    ('after_tool_result', (1, 1, 0), 'interrupted'),
    ('unknown_send', (1, 1, 1), 'needs_review'),
    ('completed', (1, 1, 1), 'completed'),
])
def test_process_crash_rebuild_never_calls_model_tools_or_delivery(tmp_path, scenario, counts, status):
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'PYTHONPATH': str(root / 'apps/backend'), 'PYTHONIOENCODING': 'utf-8'}
    def run(phase):
        return subprocess.run([sys.executable, str(root / 'tests/e2e/model_context_crash.py'),
                               str(tmp_path), scenario, phase], cwd=root, env=env,
                              capture_output=True, text=True, encoding='utf-8', timeout=40)
    crashed = run('crash')
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    reports = []
    for _ in range(2):
        recovered = run('recover')
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        reports.append(json.loads(recovered.stdout))
    assert reports[0] == reports[1]
    report = reports[0]
    assert tuple(report['counts'][key] for key in ('model', 'tool', 'send')) == counts
    assert report['status'] == status
    assert len(report['contexts']) == int(scenario != 'before_request_commit')
    if scenario in {'unknown_tool', 'unknown_send'}:
        kind = 'tool' if scenario == 'unknown_tool' else 'delivery'
        assert next(e for e in report['effects'] if e['kind'] == kind)['certainty'] == 'unknown'
