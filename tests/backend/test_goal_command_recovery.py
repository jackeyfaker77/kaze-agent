import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from core.execution.journal import ExecutionJournal
from core.goals.cli import demo_authorization, demo_definition
from core.goals.contracts import GoalConflict
from core.goals.store import GoalStore
from tests.backend.test_goals import setup


def wait_file(path):
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists()


@pytest.mark.parametrize("cancelled", [False, True])
def test_orphan_command_blocks_after_restart_and_cancellation(tmp_path, cancelled):
    script = r"""
import asyncio, os, sys
from pathlib import Path
from tests.backend.test_goals import setup, step
from core.goals.cli import demo_definition
root=Path(sys.argv[1]); inputs=root/'inputs'; inputs.mkdir()
spec=demo_definition(inputs)
spec['criteria']=[{'id':'orphan','kind':'command','timeout_seconds':30,'argv':[
sys.executable,'-I','-c',
"import pathlib,time; p=pathlib.Path.cwd(); (p/'started').touch(); deadline=time.monotonic()+15\nwhile not (p/'release').exists() and time.monotonic()<deadline: time.sleep(.02)\n(p/'late').touch()"]}]
j,s,v=setup(root,spec=spec)
async def die():
    while not (inputs/'started').exists(): await asyncio.sleep(.01)
    if sys.argv[2]=='True': s.cancel('g',expected_version=s.get('g')['version'],revoke=True)
    os._exit(74)
async def main():
    await step(s,v)
    asyncio.create_task(die())
    await step(s,v)
asyncio.run(main())
"""
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join([str(Path.cwd()), str(Path.cwd() / "apps/backend")]),
    )
    journal = None
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), str(cancelled)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        assert result.returncode == 74
        journal = ExecutionJournal(tmp_path)
        store = GoalStore(journal, tmp_path)
        store.recover()
        state = store.get("g")
        assert state["status"] == ("cancelled" if cancelled else "needs_review")
        assert state["active_phase"] is None
        rid = next(
            k
            for k, v in state["reservations"].items()
            if v["action"] == "verify:command"
        )
        before = state["budget"]
        store.create(
            "other", demo_definition(tmp_path / "inputs"), demo_authorization()
        )
        assert (
            store.claim("other", expected_version=store.get("other")["version"]) is None
        )
        assert not (tmp_path / "inputs/late").exists()
        (tmp_path / "inputs/release").touch()
        wait_file(tmp_path / "inputs/late")
        # Exit alone cannot silently clear the durable uncertainty.
        assert (
            store.claim("other", expected_version=store.get("other")["version"]) is None
        )
        with pytest.raises(ValueError):
            store.resolve_command(
                "g",
                rid,
                expected_version=state["version"],
                confirmed_stopped=False,
                note="checked",
            )
        resolved = store.resolve_command(
            "g",
            rid,
            expected_version=state["version"],
            confirmed_stopped=True,
            note="Process exited; late artifact inspected",
        )
        assert store.get("g")["budget"] == before
        assert resolved["status"] == state["status"]
        with pytest.raises(GoalConflict):
            store.resolve_command(
                "g",
                rid,
                expected_version=state["version"],
                confirmed_stopped=True,
                note="duplicate",
            )
        journal.close()
        journal = ExecutionJournal(tmp_path)
        store = GoalStore(journal, tmp_path)
        store.rebuild()
        assert store.get("g")["reservations"][rid]["command_resolution"][
            "confirmed_stopped"
        ]
        assert (
            store.claim("other", expected_version=store.get("other")["version"])
            is not None
        )
    finally:
        (tmp_path / "inputs/release").touch()
        if journal is not None:
            journal.close()


@pytest.mark.parametrize(
    "kind,action", [("model", "model"), ("validation", "verify:file")]
)
def test_other_unknown_calls_do_not_block_all_goals(tmp_path, kind, action):
    journal, store, _ = setup(tmp_path)
    try:
        claimed = store.claim("g", expected_version=store.get("g")["version"])
        assert claimed is not None
        _, phase = claimed
        store.reserve("g", phase["phase_id"], kind, action)
        store.finish("g", phase["phase_id"], {}, execution_status="needs_review")
        store.create(
            "other", demo_definition(tmp_path / "inputs"), demo_authorization()
        )
        assert (
            store.claim("other", expected_version=store.get("other")["version"])
            is not None
        )
    finally:
        journal.close()
