import asyncio
import json
from types import SimpleNamespace

import pytest

from bootstrap.app import AppRuntime, RuntimeFeatures
from bootstrap.channel_host import ChannelHost
from bootstrap.host_state import WorkspaceInUse, WorkspaceOwner, host_status, request_host_stop
from bootstrap.runtime_host import RuntimeHost
from agent.config_models import Config


class RuntimeStub:
    def __init__(self, workspace):
        self.workspace = workspace
        self.started = asyncio.Event()
        self.closed = False
        self.core = SimpleNamespace()
        self.background_tasks = []

    async def start(self):
        self.background_tasks.append(asyncio.create_task(asyncio.Event().wait(), name="worker"))
        self.started.set()

    def health(self):
        return {"healthy": self.started.is_set() and not self.closed}

    async def shutdown(self):
        self.closed = True
        for task in self.background_tasks:
            task.cancel()
        await asyncio.gather(*self.background_tasks, return_exceptions=True)


async def until(predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_host_owns_workspace_without_any_desktop_attachment(tmp_path):
    runtime = RuntimeStub(tmp_path)
    host = RuntimeHost(runtime)
    task = asyncio.create_task(host.run())
    try:
        await until(lambda: host_status(tmp_path).get("state") == "running")
        assert host_status(tmp_path)["healthy"]
        other = RuntimeStub(tmp_path)
        with pytest.raises(WorkspaceInUse):
            await RuntimeHost(other, mode="bridge").run()
        assert not other.started.is_set()
        assert not task.done()
        assert request_host_stop(tmp_path)["requested"]
        await asyncio.wait_for(task, 5)
        assert runtime.closed
        assert not host_status(tmp_path)["running"]
    finally:
        host.request_stop()
        await task


@pytest.mark.asyncio
async def test_desktop_eof_still_shuts_down_embedded_runtime(tmp_path):
    runtime = RuntimeStub(tmp_path)
    async def attachment(core):
        assert core is runtime.core
        assert host_status(tmp_path)["mode"] == "bridge"
        with pytest.raises(RuntimeError, match="桌面本地模式"):
            request_host_stop(tmp_path)
    await RuntimeHost(runtime, mode="bridge").run(attachment)
    assert runtime.closed and host_status(tmp_path)["state"] == "stopped"


@pytest.mark.asyncio
async def test_worker_failure_cleans_up_and_releases_workspace(tmp_path):
    runtime = RuntimeStub(tmp_path)
    async def broken():
        raise RuntimeError("worker broke")
    async def start():
        runtime.background_tasks.append(asyncio.create_task(broken(), name="broken"))
        runtime.started.set()
    runtime.start = start
    with pytest.raises(RuntimeError, match="worker broke"):
        await RuntimeHost(runtime).run()
    assert runtime.closed and not host_status(tmp_path)["running"]
    assert host_status(tmp_path)["state"] == "failed"
    lease = WorkspaceOwner(tmp_path, "host")
    lease.close()


@pytest.mark.asyncio
async def test_stop_during_startup_cancels_startup_and_releases_lease(tmp_path):
    runtime = RuntimeStub(tmp_path)
    cancelled = asyncio.Event()
    async def start():
        runtime.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    runtime.start = start
    host = RuntimeHost(runtime)
    task = asyncio.create_task(host.run())
    await runtime.started.wait()
    host.request_stop()
    await asyncio.wait_for(task, 5)
    assert cancelled.is_set() and runtime.closed
    assert not host_status(tmp_path)["running"]


def test_stale_pid_status_and_stop_request_cannot_stop_new_owner(tmp_path):
    first = WorkspaceOwner(tmp_path, "host")
    first.publish("running", healthy=True)
    request_host_stop(tmp_path)
    assert first.stop_requested()
    first.close()
    assert host_status(tmp_path)["state"] == "abandoned"
    second = WorkspaceOwner(tmp_path, "host")
    try:
        second.publish("running", healthy=True)
        assert not second.stop_requested()
        assert host_status(tmp_path)["owner_id"] != first.owner_id
    finally:
        second.close()


@pytest.mark.asyncio
async def test_real_app_worker_failure_does_not_skip_cleanup(tmp_path):
    app = AppRuntime(Config(provider="openai", model="fake", api_key="fake"), tmp_path, features=RuntimeFeatures(False, False))
    calls = []
    async def fail():
        raise RuntimeError("failed task")
    async def close():
        calls.append("http")
    app.http_resources.aclose = close
    app._background_tasks = [asyncio.create_task(fail())]
    await asyncio.sleep(0)
    await app.shutdown()
    assert calls == ["http"]
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        await app.start()


def test_status_does_not_expose_config_or_claim_dead_process_is_running(tmp_path):
    assert host_status(tmp_path)["state"] == "stopped"
    assert not (tmp_path / "private_runtime").exists()
    assert not request_host_stop(tmp_path)["requested"]
    owner = WorkspaceOwner(tmp_path, "host")
    owner.publish("running", healthy=True)
    owner.close()
    assert not host_status(tmp_path)["healthy"]
    assert "token" not in json.dumps(host_status(tmp_path))


@pytest.mark.asyncio
async def test_channel_polling_loss_is_visible_in_host_health(tmp_path):
    channel = SimpleNamespace(name="telegram", is_running=True)
    async def noop(*args):
        pass
    channel.start = noop
    channel.stop = noop
    channels = ChannelHost(lambda _: None)
    channels.add(channel)
    await channels.start_all()
    app = AppRuntime(Config(provider="openai", model="fake", api_key="fake"), tmp_path,
                     features=RuntimeFeatures(True, False))
    try:
        app.channel_host = channels
        app._started = True
        assert app.health()["channel_health"]["telegram"]
        channel.is_running = False
        assert not app.health()["healthy"]
        assert not app.health()["channel_health"]["telegram"]
    finally:
        await app.shutdown()
