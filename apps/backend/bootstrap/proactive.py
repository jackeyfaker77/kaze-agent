"""把 Akashic 的完整主动生命周期接入 Kaze 服务。"""

from __future__ import annotations

import logging
from dataclasses import replace
from uuid import uuid4

from agent.core.proactive_turn.gates import (
    ProactiveGateChain,
    ProactiveGateCompletion,
    ProactiveGateContext,
)
from agent.looping.ports import SessionServices
from agent.plugins.snapshot import (
    ProactiveContributions,
    ProactivePluginGeneration,
    RuntimeSnapshot,
    RuntimeSnapshotStore,
)
from agent.plugins.specs import proactive_source_key
from agent.turns.orchestrator import TurnOrchestrator, TurnOrchestratorDeps
from agent.turns.outbound import (
    OutboundDispatch,
    PushToolOutboundPort,
    sanitize_user_visible_content,
)
from core.common.timekit import utcnow
from proactive_v2.loop import ProactiveLoop
from proactive_v2.interaction_embeddings import InteractionEmbeddingCache
from proactive_v2.legacy_history import classify_legacy_deliveries
from proactive_v2.mcp_sources import SharedMcpGateway
from proactive_v2.source_catalog import workspace_sources
from proactive_v2.source_refresh import SourceRefresher
from proactive_v2.state import ProactiveStateStore

logger = logging.getLogger(__name__)


class _PushPort:
    """检查当前用户活动，并用渠道的真实回执决定是否提交主动消息。"""

    def __init__(self, runtime, loop: KazeProactiveLoop) -> None:
        self.runtime = runtime
        self.loop = loop

    def delivery_scope(self, channel: str, chat_id: str):
        target = self.runtime.push_tool.resolve_target(channel, chat_id)
        return self.runtime.bus.chat_lane.non_passive_send(channel, target)

    async def dispatch(self, outbound: OutboundDispatch) -> bool:
        target = self.runtime.push_tool.resolve_target(outbound.channel, outbound.chat_id)
        return await self.runtime.bus.chat_lane.run_send(
            outbound.channel, target, lambda: self._dispatch_now(outbound)
        )

    async def _dispatch_now(self, outbound: OutboundDispatch) -> bool:
        if not self.loop.can_send():
            return False
        push = self.runtime.push_tool
        if hasattr(push, "send"):
            receipt = await push.send(
                channel=outbound.channel,
                chat_id=outbound.chat_id,
                message=sanitize_user_visible_content(outbound.content),
                image=outbound.media[0] if outbound.media else None,
                session_key=self.loop._target_session_key(),
                commit_after_delivery=True,
                _outbound_metadata=dict(outbound.metadata),
            )
            if not receipt.ok:
                return False
            if receipt.delivery_ref:
                outbound.metadata["delivery_ref"] = receipt.delivery_ref
            for image in outbound.media[1:]:
                receipt = await push.send(
                    channel=outbound.channel,
                    chat_id=outbound.chat_id,
                    image=image,
                    session_key=self.loop._target_session_key(),
                    commit_after_delivery=True,
                )
                if not receipt.ok:
                    return False
            sent = True
        else:
            sent = await PushToolOutboundPort(push).dispatch(outbound)
        self.loop.delivered = sent
        return sent


class KazeProactiveLoop(ProactiveLoop):
    """复用完整 Kernel，并保留 Kaze 的用户活动检查和插件门控。"""

    def __init__(self, runtime, **kwargs) -> None:
        self.runtime = runtime
        self.delivered = False
        self._tick_last_user_at = None
        self._activation = None
        self._interaction_embeddings = InteractionEmbeddingCache(
            runtime.session_manager.workspace
        )
        super().__init__(**kwargs)

    def close(self) -> None:
        self._interaction_embeddings.close()
        super().close()

    async def run(self) -> None:
        assert self._runtime_snapshot_store is not None
        snapshot = self._runtime_snapshot_store.current
        routes = {
            (source.spec.server, source.spec.fetch_tool)
            for source in snapshot.proactive_sources.values()
            if source.plugin_id == "workspace"
        }
        gateway = SharedMcpGateway(self._sessions.workspace, snapshot.tool_registry)
        gateway.consumer = self._target_session_key()
        refresher = SourceRefresher(
            self._sessions.workspace,
            gateway,
            active_routes=routes,
            interval_seconds=self._cfg.feed_poller_interval_seconds,
        )
        refresher.start()
        try:
            await super().run()
        finally:
            await refresher.aclose()

    def _busy(self) -> bool:
        state = self.runtime.loop.processing_state
        return state.is_busy(self._target_session_key()) if state is not None else False

    def can_send(self) -> bool:
        if self._busy():
            return False
        current = (
            self._presence.get_last_user_at(self._target_session_key())
            if self._presence
            else None
        )
        return current == self._tick_last_user_at

    def _build_turn_orchestrator(self) -> TurnOrchestrator:
        outbound = _PushPort(self.runtime, self)
        return TurnOrchestrator(
            TurnOrchestratorDeps(
                session=SessionServices(
                    session_manager=self._sessions, presence=self._presence
                ),
                outbound=outbound,
                event_bus=self._event_bus,
                delivery_scope=outbound.delivery_scope,
            )
        )

    def _build_mcp_gateway(self):
        gateway = super()._build_mcp_gateway()
        gateway.consumer = self._target_session_key()
        return gateway

    def _build_runtime_scope(self):
        from agent.plugins.snapshot import get_current_runtime_snapshot

        scope = super()._build_runtime_scope()
        snapshot = get_current_runtime_snapshot()
        return replace(
            scope,
            shared_tools=snapshot.tool_registry if snapshot else scope.shared_tools,
            interaction_embeddings_fn=lambda now: self._interaction_embeddings.refresh(
                getattr(self._memory, "embedding_api", None),
                now=now,
            ),
        )

    def _read_workspace_proactive_context(self) -> str:
        text = (
            self._workspace_proactive_context_path().read_text(encoding="utf-8").strip()
        )
        heartbeat = self._workspace_proactive_context_path().parent / "HEARTBEAT.md"
        if heartbeat.exists():
            text += (
                "\n\n【工作区跟进指令】\n"
                + heartbeat.read_text(encoding="utf-8").strip()
            )
        if self._activation:
            text += f"\n\n【插件触发】\n{self._activation.reason}\n{dict(self._activation.metadata)}"
        return text

    async def _tick_bound(self) -> float | None:
        key = self._target_session_key()
        if self._busy():
            return None
        self.delivered = False
        self._tick_last_user_at = (
            self._presence.get_last_user_at(key) if self._presence else None
        )
        manager = getattr(self.runtime, "plugin_manager", None)
        gates = ProactiveGateChain(manager.proactive_gates if manager else [])
        result = gates.evaluate(
            ProactiveGateContext(
                tick_id=uuid4().hex,
                session_key=key,
                now_utc=utcnow(),
                target_transports=(
                    (self._cfg.default_channel, self._cfg.default_chat_id),
                ),
            )
        )
        if result.blocked:
            logger.info(
                "[proactive] 插件门控拦截 session=%s reason=%s", key, result.reason
            )
            return None
        self._activation = result.activation
        reason = "closed"
        try:
            score = await super()._tick_bound()
            reason = "sent" if self.delivered else "no_delivery"
            return score
        except BaseException:
            reason = "error"
            raise
        finally:
            self._activation = None
            if result.activation:
                gates.finalize(
                    ProactiveGateCompletion(
                        activation=result.activation,
                        session_key=key,
                        occurred_at=utcnow(),
                        outcome="delivered" if self.delivered else "closed",
                        reason=reason,
                    )
                )


def _builtin_contributions() -> dict[str, list]:
    """为没有插件管理器的嵌入式宿主提供同一组官方生命周期。"""
    from plugins.default_proactive.plugin import DefaultProactivePlugin
    from plugins.proactive_flow.plugin import ProactiveFlowPlugin
    from plugins.drift_flow.plugin import DriftFlowPlugin
    from plugins.wake_proactive.plugin import WakeProactivePlugin
    from plugins.wake_proactive_flow.plugin import WakeProactiveFlowPlugin
    from plugins.wake_drift_flow.plugin import WakeDriftFlowPlugin

    plugins = [
        DefaultProactivePlugin(),
        ProactiveFlowPlugin(),
        DriftFlowPlugin(),
        WakeProactivePlugin(),
        WakeProactiveFlowPlugin(),
        WakeDriftFlowPlugin(),
    ]
    return {
        name: [item for plugin in plugins for item in getattr(plugin, name)()]
        for name in (
            "proactive_modules",
            "proactive_lifecycles",
            "proactive_module_factories",
            "proactive_runtime_factories",
        )
    }


def build_proactive_loop(runtime) -> KazeProactiveLoop | None:
    """把当前插件能力和工作区配置绑定到完整主动循环。"""
    cfg = runtime.config.proactive
    chat_id = cfg.default_chat_id or cfg.session_key
    if not cfg.enabled or not chat_id:
        return None
    cfg = replace(cfg, default_chat_id=chat_id)
    workspace = runtime.session_manager.workspace
    classify_legacy_deliveries(runtime.session_manager)
    manager = getattr(runtime, "plugin_manager", None)
    names = (
        "proactive_modules",
        "proactive_lifecycles",
        "proactive_module_factories",
        "proactive_runtime_factories",
    )
    contributions = (
        {name: getattr(manager, name) for name in names}
        if manager
        else _builtin_contributions()
    )
    sources = list(manager.proactive_sources) if manager else []
    routes = {(source.spec.server, source.spec.fetch_tool) for source in sources}
    sources.extend(
        source
        for source in workspace_sources(workspace)
        if (source.spec.server, source.spec.fetch_tool) not in routes
    )
    roots = tuple(manager.drift_skill_roots) if manager else ()
    snapshot = RuntimeSnapshot(
        # MCP 的新增、删除和重连会更新宿主目录；主动 fetch、ACK 和 Drift 共用它。
        tool_registry=runtime.tools,
        proactive_sources={proactive_source_key(source): source for source in sources},
        proactive_modules=tuple(contributions["proactive_modules"]),
        proactive_lifecycles=tuple(contributions["proactive_lifecycles"]),
        proactive_module_factories=tuple(contributions["proactive_module_factories"]),
        proactive_runtime_factories=tuple(contributions["proactive_runtime_factories"]),
        tool_hooks=tuple(manager.tool_hooks) if manager else (),
        generations=(ProactivePluginGeneration("kaze", ProactiveContributions(roots)),),
    )
    state = ProactiveStateStore(workspace / "proactive.db")
    try:
        return KazeProactiveLoop(
            runtime,
            session_manager=runtime.session_manager,
            provider=runtime.provider,
            push_tool=runtime.push_tool,
            config=cfg,
            model=cfg.model or runtime.config.model,
            max_tokens=runtime.config.max_tokens,
            state_store=state,
            state_store_owned=True,
            memory_store=runtime.memory_runtime,
            presence=runtime.presence,
            passive_busy_fn=(runtime.loop.processing_state.is_busy if runtime.loop.processing_state else None),
            shared_tools=runtime.tools,
            event_bus=runtime.event_bus,
            runtime_snapshot_store=RuntimeSnapshotStore(snapshot),
        )
    except BaseException:
        state.close()
        raise


async def prepare_proactive_loop(runtime) -> KazeProactiveLoop | None:
    """启动时核对主动模块，并接通与普通 Agent 共用的动态 MCP 工具目录。"""
    cfg = runtime.config.proactive
    if not cfg.enabled or not (cfg.default_chat_id or cfg.session_key):
        return None
    manager = getattr(runtime, "plugin_manager", None)
    registry = getattr(runtime, "mcp_registry", None)
    if registry is not None:
        await registry.connect_declared_servers(manager.mcp_servers if manager else ())
    loop = build_proactive_loop(runtime)
    if loop is None:
        return None
    runtime.proactive_loop = loop
    try:
        await loop._start_current_snapshot()
    except BaseException:
        try:
            await loop._stop_active_kernel()
        finally:
            runtime.proactive_loop = None
            loop.close()
        raise
    return loop


async def run_proactive(runtime) -> None:
    """运行当前选择的官方主动生命周期，结束时关闭全部主动状态资源。"""
    loop = getattr(runtime, "proactive_loop", None) or await prepare_proactive_loop(
        runtime
    )
    if loop is None:
        return
    try:
        await loop.run()
    finally:
        runtime.proactive_loop = None
