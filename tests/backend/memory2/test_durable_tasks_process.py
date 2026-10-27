import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('point', [
    'source_fact', 'before_admission_commit', 'admitted', 'before_model', 'model_response',
    'before_memory_commit:mark_superseded_batch', 'after_memory_commit:mark_superseded_batch',
    'before_memory_commit:upsert_item', 'after_memory_commit:upsert_item',
    'before_done', 'done', 'wire',
])
def test_crash_recovery_keeps_task_and_never_repeats_committed_work(tmp_path, point):
    root = Path(__file__).resolve().parents[3]
    def run(phase):
        return subprocess.run([sys.executable, str(root/'tests/e2e/memory_tasks_crash.py'), str(tmp_path), point, phase],
            cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=45,
            env={**os.environ, 'PYTHONIOENCODING':'utf-8'})
    crashed = run('crash')
    assert crashed.returncode == 73, crashed.stdout + crashed.stderr
    results = []
    for _ in range(2):
        recovered = run('recover')
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        results.append(json.loads(recovered.stdout))
    assert results[0] == results[1]
    result = results[0]
    assert len(result['tasks']) == 1
    if point == 'wire':
        assert result['tasks'][0]['state'] == 'blocked'
        assert result['tasks'][0]['error'] == 'model_outcome_unknown'
        assert result['calls'] == 1
        assert result['items'] == [['old rule','active',1]]
    else:
        assert result['tasks'][0]['state'] == 'done'
        assert result['calls'] == 3
        assert result['items'] == [['new preference','active',1],['old rule','superseded',1]]
