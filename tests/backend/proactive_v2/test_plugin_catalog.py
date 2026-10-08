import pytest

from agent.plugins.manager import PluginManager
from agent.plugins.snapshot import RuntimeSnapshot, RuntimeSnapshotStore
from agent.tools.registry import ToolRegistry
from bus.event_bus import EventBus


@pytest.mark.asyncio
async def test_plugin_source_mcp_and_drift_contributions_are_collected_and_cleared(
    tmp_path,
):
    root = tmp_path / "plugins"
    plugin = root / "example"
    plugin.mkdir(parents=True)
    (plugin / "plugin.py").write_text(
        """from agent.plugins import Plugin, ProactiveSourceSpec, McpServerSpec
class Example(Plugin):
    name = "example_catalog"
    def proactive_sources(self):
        return [ProactiveSourceSpec(id="context", channels=("context",), server="sensor", fetch_tool="snapshot")]
    def mcp_servers(self):
        return [McpServerSpec(name="sensor", command=("unused",), cwd=str(self.context.plugin_dir))]
    def drift_skill_roots(self):
        return ["activities"]
""",
        encoding="utf-8",
    )
    manager = PluginManager([root], event_bus=EventBus(), workspace=tmp_path)
    try:
        await manager.load_all()
        assert manager.loaded_count == 1
        assert manager.proactive_sources[0].plugin_id == "example_catalog"
        assert manager.mcp_servers[0].name == "sensor"
        assert manager.drift_skill_roots == [(plugin / "activities").resolve()]
    finally:
        await manager.terminate_all()
    assert (
        not manager.proactive_sources
        and not manager.mcp_servers
        and not manager.drift_skill_roots
    )


@pytest.mark.asyncio
async def test_snapshot_lease_keeps_old_directory_until_its_work_finishes():
    first = RuntimeSnapshot(
        tool_registry=ToolRegistry().snapshot(), proactive_sources={}
    )
    store = RuntimeSnapshotStore(first)
    lease = await store.acquire()
    retained = lease.fork()
    second = RuntimeSnapshot(
        tool_registry=ToolRegistry().snapshot(), proactive_sources={}
    )
    await store.publish(second)
    await lease.release()
    assert retained.snapshot is first
    current = await store.acquire()
    assert current.snapshot is second
    await retained.release()
    await current.release()
    assert not store._counts
