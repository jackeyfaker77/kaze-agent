import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('scenario,counts,claimed', [
    ('source_boundary', (0, 0, 0), False),
    ('source_response', (1, 0, 0), False),
    ('tool_dispatch', (1, 0, 0), False),
    ('during_tool', (1, 1, 0), False),
    ('unknown_send', (1, 1, 1), False),
    ('physical_result', (1, 1, 1), False),
    ('semantic_result', (1, 1, 1), False),
    ('source_terminal', (1, 1, 1), False),
    ('claim_transaction', (1, 1, 1), False),
    ('after_claim', (1, 1, 1), True),
    ('new_boundary', (1, 1, 1), True),
    ('during_new_model', (2, 1, 1), True),
    ('new_response', (2, 1, 1), True),
    ('new_view', (2, 1, 1), True),
    ('new_terminal', (2, 1, 1), True),
])
def test_crash_reopen_and_repeated_projection_never_execute(tmp_path, scenario, counts, claimed):
    root = Path(__file__).resolve().parents[2]
    def run(phase):
        return subprocess.run([sys.executable, str(root / 'tests/e2e/continuation_crash.py'), str(tmp_path), scenario, phase],
            cwd=root, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, capture_output=True, text=True, encoding='utf-8', timeout=45)
    crashed = run('crash')
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    assert not (tmp_path / 'finally.txt').exists()
    reports = []
    for _ in range(2):
        result = run('recover')
        assert result.returncode == 0, result.stdout + result.stderr
        reports.append(json.loads(result.stdout))
    assert reports[0] == reports[1]
    report = reports[0]
    assert tuple(report['counts'][k] for k in ('model', 'tool', 'send')) == counts
    assert report['claims'] == int(claimed)
    if scenario in {'source_response', 'tool_dispatch', 'during_tool', 'unknown_send', 'physical_result'}:
        assert not report['plans'][0]['allowed']
    if scenario in {'source_boundary', 'semantic_result', 'source_terminal', 'claim_transaction'}:
        assert report['plans'][0]['allowed']
    if claimed:
        assert not report['plans'][0]['allowed'] and len(report['states']) == 2
    if scenario == 'after_claim':
        assert report['plans'][1]['allowed']
