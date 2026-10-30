import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[5]


@pytest.mark.parametrize('point', ['source_fact','before_admission_commit','admitted','snapshot','before_model',
    'model_response','second_model_response','draft','file_intent','file_pre_replace','file_replaced',
    'file_applied:PENDING.md','file_applied:RECENT_CONTEXT.md','file_applied:journal/2026-10-28.md',
    'files','cursor','callback','before_done','done','receipt_conflict','wire','wire_second','callback_wire',
    'callback_before_memory_commit:upsert_consolidation_event',
    'callback_after_memory_commit:upsert_consolidation_event','callback_model_response',
    'callback_after_memory_commit:upsert_item','callback_before_done'])
def test_real_crash_recovers_without_duplicate_calls_or_writes(tmp_path, point):
    command = [sys.executable, str(ROOT/'tests/e2e/markdown_tasks_crash.py'), str(tmp_path), point]
    env = {**os.environ, 'PYTHONIOENCODING':'utf-8'}
    first = subprocess.run(command+['crash'], capture_output=True, text=True, encoding='utf-8', env=env, timeout=40)
    assert first.returncode == 73, first.stdout+first.stderr
    for _ in range(2):
        recovered = subprocess.run(command+['recover'], capture_output=True, text=True, encoding='utf-8', env=env, timeout=40)
        assert recovered.returncode == 0, recovered.stdout+recovered.stderr
        result = json.loads(recovered.stdout.strip().splitlines()[-1])
        assert len(result['tasks']) == 1
        if point in {'wire', 'wire_second'}:
            assert result['calls'] == (1 if point == 'wire' else 2) and result['tasks'][0]['state'] == 'blocked'
            assert 'model_outcome_unknown' in result['tasks'][0]['error']
            assert result['history'] == result['pending'] == result['recent'] == result['journal'] == 0
        else:
            assert result['tasks'][0]['state'] == 'done', result
            assert result['calls'] == (3 if point.startswith('callback_') else 2)
            assert result['history'] == result['pending'] == result['recent'] == result['journal'] == 1
            assert result['cursor'] == 6
            if point.startswith('callback_'):
                assert result['callback_tasks'][0]['state'] == ('blocked' if point == 'callback_wire' else 'done')
                assert len(result['items']) == (1 if point == 'callback_wire' else 2)
                assert all(i['reinforcement'] == 1 for i in result['items'])
