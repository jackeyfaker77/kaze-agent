"""Desktop RPC boundary for ordinary sessions and application-wide services."""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, nullcontext
from uuid import uuid4
from typing import Any

from agent.looping.interrupt import TurnInterruptState
from core.execution.contracts import ExecutionConflict, ExecutionNeedsReview, ExecutionUnavailable, JournalWriteError
from bus.events import OutboundMessage
from bus.events_lifecycle import StreamDeltaReady, ToolCallStarted, ToolCallCompleted, ProactiveMessageCommitted, TurnCommitted
from desktop_bridge.models import BridgeError, BridgeResponse
from desktop_bridge.pending_chat_journal import PendingChatJournal
from desktop_bridge.voice.voice_handler import DesktopVoiceHandler
from desktop_bridge.voice.voice_service import VoiceService
from session.manager.models import INTERRUPTED_TURN_METADATA_KEY


@dataclass
class _PendingChatWrite:
    messages: list[dict[str, Any]] = field(default_factory=list)
    interruptions: list[TurnInterruptState] = field(default_factory=list)
    interruption_metadata: dict[str, Any] | None = None
    title: str = ""


class DesktopBridgeService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.workspace = runtime.session_manager.workspace
        self.sessions = runtime.session_manager
        from desktop_bridge.codex_service import CodexConnectionService
        self.codex = CodexConnectionService(self.workspace)
        from core.pets.packages import PetPackageService
        self.pets = PetPackageService(self.workspace)
        self._listeners = []
        self._requests = {}
        self._voice_turns = {}
        self._chat_tasks = {}
        self._chat_interrupts: dict[str, TurnInterruptState | None] = {}
        self._chat_journal = PendingChatJournal(self.workspace)
        self._pending_chat_writes = {
            key: _PendingChatWrite(messages=batch["messages"], title=batch["title"],
                                   interruption_metadata=batch["interruption_metadata"])
            for key, batch in self._chat_journal.load().items()
        }
        self._tts_cancels = {}
        self.active_session_key = "desktop:default"
        self.voice = DesktopVoiceHandler(
            workspace=self.workspace,
            voice_service=VoiceService(runtime.config.voice),
            active_runtime_configs=[],
            cancel_voice_turn=self._cancel_voice_turn,
        )
        async def committed_text(key, content):
            await self.publish_event({"id": uuid4().hex, "type": "event", "method": "message.pushed",
                                      "payload": {"session_key": key, "content": content, "session": self._snapshot(key)}})
        async def text(key, content):
            session = self.sessions.get_or_create(key)
            session.add_message("assistant", content, proactive=True)
            await self.sessions.append_messages(session, session.messages[-1:])
            await committed_text(key, content)
        async def pending_text(key, content):
            # 主动宿主负责提交历史，之后再发带完整快照的桌面通知。
            return None
        runtime.push_tool.register_channel("desktop", text=text, committed_text=committed_text, pending_text=pending_text)
        async def outbound_text(message: OutboundMessage):
            key = str(message.metadata.get("session_key_override") or message.chat_id).strip()
            await committed_text(key, message.content)
        self._outbound_handler = outbound_text
        runtime.bus.subscribe_outbound("desktop", outbound_text)
        self._event_handlers = []
        async def proactive_committed(event):
            await committed_text(event.session_key, event.assistant_response)
        runtime.event_bus.on(ProactiveMessageCommitted, proactive_committed)
        self._event_handlers.append((ProactiveMessageCommitted, proactive_committed))
        async def external_committed(event):
            if event.channel != "desktop":
                await committed_text(event.session_key, event.assistant_response)
        runtime.event_bus.on(TurnCommitted, external_committed)
        self._event_handlers.append((TurnCommitted, external_committed))
        for event_type, method in (
            (StreamDeltaReady, "chat.delta"),
            (ToolCallStarted, "chat.tool.started"),
            (ToolCallCompleted, "chat.tool.completed"),
        ):
            async def handler(event, method=method):
                request_id = self._requests.get(event.session_key)
                if request_id:
                    await self.publish_event({"id": request_id, "type": "event",
                                              "method": method, "payload": asdict(event)})
            runtime.event_bus.on(event_type, handler)
            self._event_handlers.append((event_type, handler))

    def start_background_tasks(self):
        # Clone retirement is explicit; migration must never delete remote voices.
        pass

    async def recover_pending_chat_writes(self):
        """Recover before accepting RPCs; a still-unavailable database keeps its journal."""
        for key in list(self._pending_chat_writes):
            try:
                await self._flush_pending_chat_writes(key)
            except Exception:
                logging.getLogger(__name__).exception("Pending chat recovery failed for %s", key)

    def add_event_listener(self, listener):
        self._listeners.append(listener)

    def remove_event_listener(self, listener):
        self._listeners.remove(listener)

    @property
    def has_event_listeners(self):
        return bool(self._listeners)

    async def publish_event(self, event):
        for listener in tuple(self._listeners):
            result = listener(event)
            if inspect.isawaitable(result):
                await result

    def _key(self, payload):
        key = str(payload.get("session_key") or payload.get("chat_id") or "").strip()
        if not key:
            raise ValueError("session_key 或 chat_id 不能为空")
        return key

    def _snapshot(self, key):
        session = self.sessions.get_or_create(key)
        return {"session_key": key, "title": session.metadata.get("title", key),
                "messages": session.messages, "metadata": session.metadata}

    def _cancel_chat(self, key):
        task = self._chat_tasks.get(key)
        if task is None or task.done():
            return False
        # Repeated cancels must not interrupt the pending snapshot write.
        if key in self._chat_interrupts:
            return True
        state = self.runtime.loop.active_turn_states.get(key)
        snapshot = None
        # A desktop request may still be waiting behind another turn's session lock.
        if state is not None and state.original_metadata.get("request_id") == self._requests[key]:
            snapshot = self.runtime.loop.request_interrupt(
                key, sender="desktop", command="chat.cancel",
            ).state
        self._chat_interrupts[key] = snapshot
        if snapshot is None:
            task.cancel()
        return True

    def request_chat_shutdown(self):
        """Capture partial output before the host cancels its request dispatcher."""
        for key in tuple(self._chat_tasks):
            self._cancel_chat(key)

    async def _persist_interrupted_turn(self, key, request_id, content, media, metadata, previous_message_ids):
        state = self._chat_interrupts.get(key)
        session = self.sessions.get_or_create(key)
        pending = self._pending_chat_writes.setdefault(key, _PendingChatWrite())
        turn_messages = [
            message for message in session.messages
            if message.get("id") not in previous_message_ids
            and (message.get("metadata") or {}).get("request_id") == request_id
            and not any(message is retained for retained in pending.messages)
        ]
        # A completed reply may already be waiting for its append. Preserve it;
        # only construct a partial reply when no assistant message exists yet.
        if not any(message.get("role") == "assistant" for message in turn_messages):
            metadata = {**metadata, "source": "desktop"}
            if not turn_messages:
                session.add_message("user", content, media=media, metadata=metadata)
                turn_messages.append(session.messages[-1])
            if state is not None and (state.partial_reply or state.partial_thinking or state.tool_chain_partial):
                session.add_message(
                    "assistant", state.partial_reply,
                    reasoning_content=state.partial_thinking,
                    tools_used=list(state.tools_used) or None,
                    tool_chain=list(state.tool_chain_partial) or None,
                    metadata={**metadata, "interrupted_reply": True},
                )
                turn_messages.append(session.messages[-1])
                session.metadata[INTERRUPTED_TURN_METADATA_KEY] = {
                    "request_id": request_id, "interrupted_by": state.interrupted_by,
                }
        for message in turn_messages:
            # A store can commit a row and then fail before returning its ID.
            # Keep a stable identity across retries, independent of the RPC ID.
            if not message.get("id"):
                message.setdefault("desktop_write_id", uuid4().hex)
            pending.messages.append(message)
        if state is not None and not any(state is retained for retained in pending.interruptions):
            pending.interruptions.append(state)
        if INTERRUPTED_TURN_METADATA_KEY in session.metadata:
            pending.interruption_metadata = dict(session.metadata[INTERRUPTED_TURN_METADATA_KEY])
        pending.title = session.metadata.get("title", key)
        await self._flush_pending_chat_writes(key)

    async def _flush_pending_chat_writes(self, key):
        pending = self._pending_chat_writes.get(key)
        if pending is None:
            return
        # Persist the entire batch BEFORE touching SQLite. A process can exit at
        # any subsequent point; committed rows are reconciled by desktop_write_id.
        self._chat_journal.save(key, pending.messages, pending.title, pending.interruption_metadata)
        # Reconcile committed rows even if the cache was invalidated after failure.
        saved = {
            row["desktop_write_id"]: row
            for row in self.sessions._store.fetch_session_messages(key)
            if row.get("desktop_write_id")
        }
        session = self.sessions.get_or_create(key)
        for message in pending.messages:
            if row := saved.get(message.get("desktop_write_id")):
                message.update(row)
            if not any(message is existing or (message.get("id") and message["id"] == existing.get("id"))
                       for existing in session.messages):
                session.messages.append(message)
        if not session.metadata.get("title"):
            session.metadata["title"] = pending.title
        if pending.interruption_metadata is not None:
            session.metadata[INTERRUPTED_TURN_METADATA_KEY] = dict(pending.interruption_metadata)
        # Retain the batch and interruption context until the entire write succeeds.
        await self.sessions.append_messages(session, pending.messages)
        self._chat_journal.remove(key)
        for state in pending.interruptions:
            self.runtime.loop.discard_interrupt_state(key, state)
        self._pending_chat_writes.pop(key, None)

    def _cancel_voice_turn(self, turn_id):
        key = self._voice_turns.get(turn_id)
        cancel = self._tts_cancels.get(turn_id)
        if cancel:
            cancel.set()
        return self._cancel_chat(key) if key else False

    # [消息链路 6/6] Python 业务 RPC 入口，由 server.py::DesktopBridgeServer 调用：
    # 解析 method + payload，路由到会话、记忆或 AgentLoop，并包装统一响应。
    async def handle(self, request, *, emit_event=None, chat_admission: Callable[[], AbstractAsyncContextManager[None]] = nullcontext):
        request_id = str(request.get("id") or uuid4())
        # Electron 传入的请求统一由 method 和 payload 描述。
        method = str(request.get("method") or "")
        payload = request.get("payload") or {}
        try:
            if not isinstance(payload, dict):
                raise ValueError("payload 必须是对象")
            # chat.send、session.get、memory.get 等请求共用同一个路由入口。
            result = await self._dispatch(method, payload, request_id, chat_admission=chat_admission)
            if method in {"session.get", "session.create", "session.draft"}:
                await self.publish_event({"id": request_id, "type": "event", "method": "session.selected", "payload": {"session_key": self.active_session_key}})
            return BridgeResponse(id=request_id, type="response", method=method, payload=result)
        except asyncio.CancelledError:
            raise
        except ExecutionNeedsReview as exc:
            return BridgeResponse(id=request_id, type="response", method=method,
                                  error=BridgeError(code="execution_needs_review", message=str(exc), details=exc.details))
        except ExecutionUnavailable as exc:
            return BridgeResponse(id=request_id, type="response", method=method,
                                  error=BridgeError(code="execution_unavailable", message=str(exc), details=exc.details))
        except JournalWriteError as exc:
            return BridgeResponse(id=request_id, type="response", method=method,
                                  error=BridgeError(code="execution_outcome_unknown", message=str(exc)))
        except Exception as exc:
            return BridgeResponse(id=request_id, type="response", method=method,
                                  error=BridgeError(code="request_failed", message=str(exc)))

    async def _dispatch(self, method, payload, request_id, *, chat_admission: Callable[[], AbstractAsyncContextManager[None]] = nullcontext, retry_request=None):
        if method in {'transport.list', 'transport.get', 'transport.review'}:
            store = self.runtime.bus.transport
            if store is None:
                raise ValueError('持久收发队列未启用')
            journal = self.sessions.execution_journal
            if method == 'transport.list':
                return store.list_jobs(journal=journal, session_key=str(payload.get('session_key') or ''),
                    turn_id=str(payload.get('turn_id') or ''), status=payload.get('status', 'attention'),
                    before=payload.get('before', 0), limit=payload.get('limit', 25))
            box = self.runtime.bus.outbox
            options = dict(journal=journal, accounts=box.accounts if box else {}, handlers=box.handlers if box else {})
            key = str(payload.get('job_id') or '')
            if method == 'transport.get':
                return store.detail(key, **options)
            return store.review_job(key, expected_revision=payload.get('expected_revision'),
                action_id=payload.get('action_id'), decision=payload.get('decision'), note=payload.get('note'),
                absent_parts=payload.get('absent_parts'), **options)
        if method == "execution.review":
            store = getattr(self.runtime.bus, 'transport', None)
            if store is not None and payload.get('decision') == 'no_effect':
                store.require_execution_retry_clearance(str(payload.get('turn_id') or ''), self.sessions.execution_journal)
            return self.sessions.execution_journal.review(str(payload.get("turn_id") or ""),
                expected_seq=payload.get("expected_seq"), action_id=payload.get("action_id"),
                decision=payload.get("decision"), note=payload.get("note"))
        if method == "execution.retry":
            original = self.sessions.execution_journal.get(str(payload.get("turn_id") or ""))
            store = getattr(self.runtime.bus, 'transport', None)
            if store is not None and not original['retry_turn_id']:
                store.require_execution_retry_clearance(original['turn_id'], self.sessions.execution_journal)
            if original["source"] != "desktop" or original["input"].get("channel") != "desktop":
                raise ValueError("此来源由原渠道队列管理，不能从桌面重发")
            saved = original["input"]
            result = await self._dispatch("chat.send", {"session_key": original["session_key"],
                "content": saved["content"], "media": saved["media"]}, request_id,
                chat_admission=chat_admission, retry_request={"original": original,
                    "action": {"turn_id": original["turn_id"], "expected_seq": payload.get("expected_seq"),
                               "action_id": payload.get("action_id")}})
            return {**result, "execution": self.sessions.execution_journal.get(original["turn_id"])}
        if method == "execution.get":
            journal = self.sessions.execution_journal
            if payload.get("turn_id"):
                return journal.get(str(payload["turn_id"]))
            record = journal.find(self._key(payload), str(payload.get("source") or "desktop"), str(payload.get("execution_request_id") or ""))
            if record is None:
                raise ValueError("未找到该执行请求")
            return record
        if method == "execution.events":
            return self.sessions.execution_journal.events(str(payload.get("turn_id") or ""),
                after=payload.get("after", 0), limit=payload.get("limit", 100))
        if method == "executions.list":
            return self.sessions.execution_journal.list_executions(str(payload.get("session_key") or ""),
                before=payload.get("before", 0), limit=payload.get("limit", 50), status=payload.get("status", ""))
        if method.startswith("codex."):
            return await self.codex.handle(method, payload)
        if method == "health":
            return {"ok": True, "architecture": "sessions", "memory_path": str(self.workspace / "memory"),
                    "execution_contract_version": 1, "root_id": self.sessions.execution_journal.root_id,
                    "epoch": self.sessions.execution_journal.epoch,
                    "pending_chat_writes": len(self._pending_chat_writes)}
        voice_result = await self.voice.handle(method, payload)
        if voice_result is not None:
            return voice_result
        if method == "observation.analyze":
            return await self.runtime.screen_observation.analyze(payload)
        if method == "observation.remember":
            return await self.runtime.screen_observation.remember(payload)
        if method == "pet.get":
            return {**self.pets.snapshot(), "session_key": self.active_session_key}
        if method == "pet.import":
            return self.pets.import_package(str(payload.get("path") or ""))
        if method == "pet.select":
            return self.pets.select(str(payload.get("package_id") or ""))
        if method == "sessions.list":
            return {"sessions": [{**item, "metadata": self.sessions.get_or_create(item["key"]).metadata,
                                   "message_count": len(self.sessions.get_or_create(item["key"]).messages)}
                                  for item in self.sessions.list_sessions()]}
        if method == "session.draft":
            # Selecting a composer must not create an empty database record.
            # The same key also routes desktop-pet voice into the eventual session.
            self.active_session_key = self._key(payload)
            return {"session_key": self.active_session_key}
        if method == "messages.list":
            rows, total = self.sessions._store.list_messages_for_admin(
                session_key=payload.get("session_key") or None,
                q=str(payload.get("q") or ""), role=str(payload.get("role") or ""),
                page=int(payload.get("page") or 1), page_size=50,
            )
            return {"messages": rows, "total": total}
        if method == "runtime.catalog":
            from agent.skills import SkillsLoader
            documents = [
                {"id": "MEMORY.md", "name": "长期记忆", "description": "沉淀的长期事实、偏好与经验。"},
                {"id": "SELF.md", "name": "自我认知", "description": "Agent 对自身状态与能力边界的认识。"},
                {"id": "HISTORY.md", "name": "历史摘要", "description": "跨会话保留的历史与工作记录。"},
            ]
            return {"documents": documents, "skills": SkillsLoader(self.workspace).list_skills(),
                    "tools": [asdict(tool) for tool in self.runtime.tools.get_documents()],
                    "servers": self.runtime.mcp_registry.list_servers()}
        if method == "document.get":
            name = str(payload.get("id") or "")
            if name not in {"MEMORY.md", "SELF.md", "HISTORY.md"}:
                raise ValueError("未知文档")
            path = self.workspace / "memory" / name
            return {"content": path.read_text(encoding="utf-8") if path.exists() else "", "path": f"memory/{name}"}
        if method == "session.create":
            key = str(payload.get("session_key") or f"desktop:{uuid4().hex}").strip()
            self.active_session_key = key
            session = self.sessions.get_or_create(key)
            session.metadata.setdefault("title", str(payload.get("title") or "新会话"))
            await self.sessions.save_async(session)
            return self._snapshot(key)
        if method == "session.get":
            self.active_session_key = self._key(payload)
            return self._snapshot(self.active_session_key)
        if method == "session.rename":
            key = self._key(payload)
            title = str(payload.get("title") or "").strip()
            if not title:
                raise ValueError("会话名称不能为空")
            session = self.sessions.get_or_create(key)
            session.metadata["title"] = title
            await self.sessions.save_async(session)
            return self._snapshot(key)
        if method == "session.delete":
            key = self._key(payload)
            if key in self._requests:
                raise ValueError("请先停止正在运行的会话")
            if key in self._pending_chat_writes:
                raise ValueError("此会话有保存失败的消息，请先重试保存后再删除")
            self.sessions.invalidate(key)
            return {"deleted": self.sessions._store.delete_session(key, cascade=True)}
        if method == "chat.cancel":
            key = self._key(payload)
            return {"cancelled": self._cancel_chat(key)}
        if method == "chat.send":
            key = self._key(payload)
            content = str(payload.get("content") or "").strip()
            media = payload.get("media") or []
            if not isinstance(media, list) or any(not isinstance(path, str) for path in media):
                raise ValueError("media 必须是文件路径列表")
            if not content and not media:
                raise ValueError("消息不能为空")
            # 同一个 session_key 同时只允许一个回复任务，避免会话历史并发写入。
            if key in self._requests:
                raise ValueError("此会话正在回复，请等待或停止后重试")
            credential = self.runtime.config.api_key.strip()
            if credential in {"sk-...", "YOUR_API_KEY", "your-api-key"} or credential.startswith("${"):
                raise ValueError(f"模型 {self.runtime.config.model} 的 API Key 尚未配置，请在“模型”中填写有效密钥或选择已配置的模型。")
            # session_key 用于获取或创建彼此隔离的会话状态。
            session = self.sessions.get_or_create(key)
            previous_message_ids = {message["id"] for message in session.messages if message.get("id")}
            if not session.metadata.get("title") or session.metadata["title"] == "新会话":
                session.metadata["title"] = content.splitlines()[0][:60] if content else "附件会话"
            self._requests[key] = request_id
            self._chat_tasks[key] = asyncio.current_task()
            voice_turn = str(payload.get("voice_turn_id") or "")
            if voice_turn:
                self._voice_turns[voice_turn] = key
            metadata = {"request_id": request_id, "input_method": payload.get("input_method", "text")}
            metadata["execution_request_id"] = uuid4().hex
            if "execution_request_id" in payload:
                metadata["execution_request_id"] = payload["execution_request_id"]
            if retry_request is not None:
                metadata = {**retry_request["original"]["input"]["metadata"], "request_id": request_id,
                            "_execution_retry": retry_request["action"]}
            async with AsyncExitStack() as turn_context:
                try:
                    # Keep passive priority through cancellation persistence, including
                    # requests still waiting for an integration execution slot.
                    await turn_context.enter_async_context(self.runtime.bus.chat_lane.passive_turn("desktop", key))
                    async with chat_admission():
                        await self._flush_pending_chat_writes(key)
                        session = self.sessions.get_or_create(key)
                        previous_message_ids = {message["id"] for message in session.messages if message.get("id")}
                        # 把桌面消息交给 AgentLoop；stream_events=True 会持续发布增量与工具事件。
                        reply = await self.runtime.loop.process_direct(
                            content=content, session_key=key, channel="desktop", chat_id=key,
                            stream_events=True, media=media,
                            raise_on_error=True,
                            metadata=metadata,
                        )
                        interrupted = self._chat_interrupts.get(key)
                        if interrupted is not None:
                            # A provider may finish normally while handling cancellation.
                            self.runtime.loop.discard_interrupt_state(key, interrupted)
                        result = {"session_key": key, "reply": reply, "session": self._snapshot(key)}
                        # 推理结束后发布完整 Session，Renderer 据此收敛 optimistic update 与流式状态。
                        await self.publish_event({"id": request_id, "type": "event", "method": "chat.done", "payload": result})
                        if voice_turn:
                            await self._speak_reply(key, voice_turn, request_id, reply)
                        return result
                except asyncio.CancelledError:
                    await self._persist_interrupted_turn(key, request_id, content, media, metadata, previous_message_ids)
                    return {"session_key": key, "cancelled": True, "session": self._snapshot(key)}
                except Exception as exc:
                    # A committed run can outlive its reply stream.
                    if isinstance(exc, (ExecutionConflict, ExecutionUnavailable, JournalWriteError)):
                        raise
                    journal = self.sessions.execution_journal
                    if retry_request is not None:
                        parent = journal.get(retry_request["action"]["turn_id"])
                        accepted = journal.get(parent["retry_turn_id"]) if parent["retry_turn_id"] else None
                    else:
                        accepted = journal.find(key, "desktop", metadata["execution_request_id"])
                    if accepted is not None and accepted["status"] == "completed":
                        raise ExecutionUnavailable("执行已完成，但回复未成功返回；请查看执行记录，勿重新执行", accepted) from exc
                    raise
                finally:
                    self._requests.pop(key, None)
                    self._chat_tasks.pop(key, None)
                    self._chat_interrupts.pop(key, None)
                    self._voice_turns.pop(voice_turn, None)
        if method == "tasks.list":
            key = payload.get("session_key")
            return {"tasks": [json.loads(json.dumps(asdict(job), default=lambda value: value.isoformat())) for job in self.runtime.scheduler.list_jobs()
                              if not key or job.session_key == key]}
        if method == "memory.get":
            return {"content": self.runtime.memory_runtime.markdown.store.read_long_term(),
                    "path": str(self.workspace / "memory" / "MEMORY.md")}
        if method == "memory.save":
            async with self.runtime.memory_runtime.markdown.maintenance._global_write_lock:
                self.runtime.memory_runtime.markdown.store.write_long_term(str(payload.get("content") or ""))
            return {"saved": True}
        raise ValueError(f"未知方法: {method}")

    async def _speak_reply(self, key, turn_id, request_id, reply):
        config = self.runtime.config.voice
        enabled = bool(config.enabled and config.tts.enabled and config.tts.voice_id)
        base = {"session_key": key, "voice_turn_id": turn_id, "request_id": request_id}
        async def emit(method, **extra):
            await self.publish_event({"id": request_id, "type": "event", "method": method, "payload": {**base, **extra}})
        await emit("voice.reply.started", has_voice=enabled)
        if not enabled:
            await emit("voice.tts.finished")
            return
        cancel = threading.Event()
        self._tts_cancels[turn_id] = cancel
        try:
            audio = await asyncio.to_thread(self.voice.voice_service.synthesize,
                                           reply, voice_id=config.tts.voice_id, speed=1.0, cancel_event=cancel)
            await emit("voice.tts.audio", sequence=0, text=reply, audio_base64=base64.b64encode(audio).decode("ascii"))
        except Exception as exc:
            await emit("voice.tts.error", message=str(exc))
        finally:
            cancel.set()
            self._tts_cancels.pop(turn_id, None)
            await emit("voice.tts.finished")

    async def aclose(self):
        self.runtime.bus.unsubscribe_outbound("desktop", self._outbound_handler)
        await self.codex.aclose()
        for event_type, handler in self._event_handlers:
            self.runtime.event_bus.off(event_type, handler)
        await self.voice.aclose()
