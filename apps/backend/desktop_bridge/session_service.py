"""Desktop RPC boundary for ordinary sessions and application-wide services."""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import threading
from dataclasses import asdict
from uuid import uuid4

from bus.events_lifecycle import StreamDeltaReady, ToolCallStarted, ToolCallCompleted
from desktop_bridge.models import BridgeError, BridgeResponse
from desktop_bridge.voice.voice_handler import DesktopVoiceHandler
from desktop_bridge.voice.voice_service import VoiceService


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
        runtime.push_tool.register_channel("desktop", text=text, committed_text=committed_text)
        self._event_handlers = []
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

    def _cancel_voice_turn(self, turn_id):
        key = self._voice_turns.get(turn_id)
        task = self._chat_tasks.get(key) if key else None
        cancel = self._tts_cancels.get(turn_id)
        if cancel:
            cancel.set()
        if task:
            task.cancel()
        return task is not None

    async def handle(self, request, *, emit_event=None):
        request_id = str(request.get("id") or uuid4())
        method = str(request.get("method") or "")
        payload = request.get("payload") or {}
        try:
            if not isinstance(payload, dict):
                raise ValueError("payload 必须是对象")
            result = await self._dispatch(method, payload, request_id)
            if method in {"session.get", "session.create", "session.draft"}:
                await self.publish_event({"id": request_id, "type": "event", "method": "session.selected", "payload": {"session_key": self.active_session_key}})
            return BridgeResponse(id=request_id, type="response", method=method, payload=result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return BridgeResponse(id=request_id, type="response", method=method,
                                  error=BridgeError(code="request_failed", message=str(exc)))

    async def _dispatch(self, method, payload, request_id):
        if method.startswith("codex."):
            return await self.codex.handle(method, payload)
        if method == "health":
            return {"ok": True, "architecture": "sessions", "memory_path": str(self.workspace / "memory")}
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
            self.sessions.invalidate(key)
            return {"deleted": self.sessions._store.delete_session(key, cascade=True)}
        if method == "chat.cancel":
            key = self._key(payload)
            task = self._chat_tasks.get(key)
            if task:
                task.cancel()
            return {"cancelled": task is not None}
        if method == "chat.send":
            key = self._key(payload)
            content = str(payload.get("content") or "").strip()
            media = payload.get("media") or []
            if not isinstance(media, list) or any(not isinstance(path, str) for path in media):
                raise ValueError("media 必须是文件路径列表")
            if not content and not media:
                raise ValueError("消息不能为空")
            if key in self._requests:
                raise ValueError("此会话正在回复，请等待或停止后重试")
            credential = self.runtime.config.api_key.strip()
            if credential in {"sk-...", "YOUR_API_KEY", "your-api-key"} or credential.startswith("${"):
                raise ValueError(f"模型 {self.runtime.config.model} 的 API Key 尚未配置，请在“模型”中填写有效密钥或选择已配置的模型。")
            session = self.sessions.get_or_create(key)
            if not session.metadata.get("title") or session.metadata["title"] == "新会话":
                session.metadata["title"] = content.splitlines()[0][:60] if content else "附件会话"
            self._requests[key] = request_id
            self._chat_tasks[key] = asyncio.current_task()
            voice_turn = str(payload.get("voice_turn_id") or "")
            if voice_turn:
                self._voice_turns[voice_turn] = key
            try:
                reply = await self.runtime.loop.process_direct(
                    content=content, session_key=key, channel="desktop", chat_id=key,
                    stream_events=True, media=media,
                    raise_on_error=True,
                    metadata={"request_id": request_id, "input_method": payload.get("input_method", "text")},
                )
                result = {"session_key": key, "reply": reply, "session": self._snapshot(key)}
                await self.publish_event({"id": request_id, "type": "event", "method": "chat.done", "payload": result})
                if voice_turn:
                    await self._speak_reply(key, voice_turn, request_id, reply)
                return result
            except asyncio.CancelledError:
                return {"session_key": key, "cancelled": True, "session": self._snapshot(key)}
            finally:
                self._requests.pop(key, None)
                self._chat_tasks.pop(key, None)
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
        await self.codex.aclose()
        for event_type, handler in self._event_handlers:
            self.runtime.event_bus.off(event_type, handler)
        await self.voice.aclose()
