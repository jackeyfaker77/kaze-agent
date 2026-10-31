"""Regression tests for the independently reviewed P0b boundaries."""
import asyncio
from types import SimpleNamespace
import sys
from unittest.mock import patch

import pytest

from agent.provider import LLMProvider
from core.goals.cli import demo_authorization, demo_definition
from core.goals.contracts import GoalBlocked
from core.goals.executor import BudgetProvider, GoalReadTool
from core.goals.service import GoalContext
from tests.backend.test_goals import setup, step


async def test_revocation_fences_actual_provider_retry(tmp_path):
    journal, store, _ = setup(tmp_path)
    try:
        claimed = store.claim('g', expected_version=store.get('g')['version'])
        assert claimed is not None
        state, phase = claimed
        context = GoalContext(store, state, phase)
        first = asyncio.Event()
        calls = []
        async def create(**kwargs):
            calls.append(1)
            first.set()
            raise TimeoutError('fake timeout')
        provider = object.__new__(LLMProvider)
        provider._max_retries = 1
        provider._request_timeout_s = 3
        provider._payload_snapshot_enabled = False
        provider._base_url = 'fake'
        provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        async def chat(**kwargs):
            return await provider._create_with_retry(kwargs)
        provider.chat = chat
        with patch('agent.provider.projected_request', lambda k, t: k), patch('agent.provider._save_llm_payload_snapshot'):
            task = asyncio.create_task(BudgetProvider(provider, context).chat(model='fake'))
            await first.wait()
            store.cancel('g', expected_version=store.get('g')['version'], revoke=True)
            with pytest.raises(GoalBlocked):
                await task
        assert len(calls) == 1
        assert store.get('g')['budget']['held']['model'] == 2
        from core.execution.model_recording import model_attempt_guard
        assert model_attempt_guard.get() is None
    finally:
        journal.close()


async def test_known_read_failure_is_charged_and_allows_correction(tmp_path):
    journal, store, _ = setup(tmp_path)
    try:
        claimed = store.claim('g', expected_version=store.get('g')['version'])
        assert claimed is not None
        state, phase = claimed
        context = GoalContext(store, state, phase)
        tool = GoalReadTool(context, state)
        assert 'FileNotFoundError' in await tool.execute('missing.txt')
        assert 'ValueError' in await tool.execute('../outside.txt')
        context.artifact_dir.mkdir(parents=True)
        (context.artifact_dir / 'valid.txt').write_text('corrected')
        assert await tool.execute('valid.txt') == 'corrected'
        assert store.get('g')['budget']['used']['tool'] == 3
        assert store.get('g')['budget']['held']['tool'] == 0
    finally:
        journal.close()


async def test_cancelled_verifier_retains_ownership_until_receipt(tmp_path):
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    spec = demo_definition(inputs)
    spec['criteria'] = [{'id': 'bounded', 'kind': 'command', 'timeout_seconds': 10,
        'argv': [sys.executable, '-I', '-c',
            'import pathlib,time; p=pathlib.Path.cwd(); (p/"started").touch(); '
            '\nwhile not (p/"release").exists(): time.sleep(.01)'
            '\n(p/"finished").touch(); print("verified")']}]
    journal, store, service = setup(tmp_path, spec=spec)
    task = None
    try:
        await step(store, service)
        store.create('other', demo_definition(inputs), demo_authorization())
        task = asyncio.create_task(step(store, service))
        async def started():
            while not (inputs/'started').exists():
                await asyncio.sleep(.01)
        await asyncio.wait_for(started(), 5)
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done()
        assert store.get('g')['active_phase'] is not None
        assert store.claim('other', expected_version=store.get('other')['version']) is None
        (inputs/'release').touch()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (inputs/'finished').exists()
        result = store.get('g')
        assert result['active_phase'] is None
        receipts = [r for r in result['reservations'].values() if r['kind'] == 'validation']
        assert receipts[0]['status'] == 'settled'
        assert receipts[0]['receipt']['result']['returncode'] == 0
        assert result['budget']['held']['validation'] == 0
    finally:
        (inputs/'release').touch()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        journal.close()
