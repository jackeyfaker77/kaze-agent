"""Authenticated desktop transport owned by the Host, never by a connection.

Listen only on loopback; publish through an authenticated TLS reverse proxy or
SSH tunnel. One service/dispatcher owns all sessions and accepted requests.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from pathlib import Path
from uuid import uuid4

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from desktop_bridge.models import BridgeError, BridgeResponse
from desktop_bridge.request_dispatcher import BridgeRequestDispatcher
from desktop_bridge.session_service import DesktopBridgeService

REMOTE_METHODS = frozenset({
    "health", "sessions.list", "session.get", "session.draft", "session.create",
    "session.rename", "session.delete", "messages.list", "chat.send", "chat.cancel",
    "tasks.list", "memory.get", "memory.save", "runtime.catalog", "document.get",
    "execution.get", "execution.events", "execution.context", "executions.list", "execution.review", "execution.retry", "execution.plan", "execution.continue",
    "transport.list", "transport.get", "transport.review",
})
MUTATIONS = frozenset({"session.create", "session.rename", "session.delete", "chat.send", "chat.cancel", "execution.review", "execution.retry", "execution.continue", "transport.review"})


def read_remote_token(path: Path) -> str:
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32 or len(token) > 512 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("Runtime token must contain 32–512 non-whitespace ASCII characters")
    return token


class RemoteHostServer:
    def __init__(self, runtime, token: str, *, port: int):
        self.service = DesktopBridgeService(runtime)
        self.token = token
        self.port = port
        self.ready = asyncio.Event()
        self.dispatcher = BridgeRequestDispatcher()
        self.clients: dict[ServerConnection, asyncio.Queue] = {}
        self.pending = 0
        self.connections = 0

    async def broadcast(self, event):
        # Selection belongs to each desktop, not to the shared service.
        if event["method"] == "session.selected":
            return
        for socket, queue in tuple(self.clients.items()):
            self.enqueue(socket, queue, event)

    def enqueue(self, socket, queue, value):
        try:
            queue.put_nowait(json.dumps(value, ensure_ascii=False))
        except asyncio.QueueFull:
            # A slow desktop cannot block a model turn or another subscriber.
            socket.transport.close()

    async def writer(self, socket, queue):
        try:
            while True:
                await socket.send(await queue.get())
        except ConnectionClosed:
            return

    async def handle(self, socket: ServerConnection):
        if self.connections >= 16:
            await socket.close(1013, "Host connection limit")
            return
        self.connections += 1
        try:
            await self._handle(socket)
        finally:
            self.connections -= 1

    async def _handle(self, socket: ServerConnection):
        try:
            raw = await asyncio.wait_for(socket.recv(), 5)
            auth = json.loads(raw)
            if (not isinstance(auth, dict) or auth.get("type") != "auth"
                    or not isinstance(auth.get("token"), str)
                    or not hmac.compare_digest(auth["token"].encode(), self.token.encode())):
                await socket.close(1008, "Authentication failed")
                return
        except (TimeoutError, ValueError, ConnectionClosed):
            await socket.close(1008, "Authentication required")
            return
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.clients[socket] = queue
        sender = asyncio.create_task(self.writer(socket, queue))
        prefix = uuid4().hex
        active_ids: set[str] = set()
        self.enqueue(socket, queue, {"type": "auth.ok", "protocol": 1})
        try:
            async for raw in socket:
                try:
                    request = json.loads(raw)
                    if not isinstance(request, dict):
                        raise ValueError()
                    request_id, method, payload = request.get("id"), request.get("method"), request.get("payload")
                    if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128
                            or not isinstance(method, str) or not isinstance(payload, dict)):
                        raise ValueError()
                except (ValueError, TypeError):
                    await socket.close(1008, "Invalid request envelope")
                    break
                if request_id in active_ids:
                    await socket.close(1008, "Duplicate active request ID")
                    break
                error = None
                if method not in REMOTE_METHODS:
                    error = "此功能尚不支持远程模式"
                elif method == "chat.send" and (payload.get("media") or payload.get("voice_turn_id")):
                    error = "远程模式暂不支持本地附件或语音，请发送文字"
                elif self.pending >= 128:
                    error = "Host 请求队列已满，请稍后重试"
                if error:
                    self.enqueue(socket, queue, BridgeResponse(id=request_id, type="response", method=method,
                        error=BridgeError(code="remote_rejected", message=error)).to_dict())
                    continue
                self.pending += 1
                active_ids.add(request_id)
                internal = {**request, "id": f"{prefix}:{request_id}"}

                async def execute(request=internal, original_id=request_id):
                    try:
                        result = await self.service.handle(request, chat_admission=self.dispatcher.integration_slot)
                        result.id = original_id
                        if request["method"] == "health":
                            result.payload.update({"transport": "remote", "protocol": 1})
                        self.enqueue(socket, queue, result.to_dict())
                        if not result.error and request["method"] in MUTATIONS:
                            await self.broadcast({"id": uuid4().hex, "type": "event", "method": "sessions.changed", "payload": {}})
                    finally:
                        self.pending -= 1
                        active_ids.discard(original_id)

                self.dispatcher.submit(internal, execute)
                await asyncio.sleep(0)
        except ConnectionClosed:
            pass
        finally:
            self.clients.pop(socket, None)
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            # Accepted work belongs to the Host and survives this disconnection.

    async def run(self):
        self.service.add_event_listener(self.broadcast)
        try:
            await self.service.recover_pending_chat_writes()
            transport_logger = logging.getLogger("runtime.remote.websocket")
            # websockets DEBUG frame logging would include the authentication frame.
            transport_logger.setLevel(logging.INFO)
            async with serve(self.handle, "127.0.0.1", self.port, origins=[None],
                             max_size=1024 * 1024, max_queue=16, compression=None,
                             close_timeout=2, ping_interval=20, ping_timeout=20, logger=transport_logger) as server:
                self.port = list(server.sockets)[0].getsockname()[1]
                self.ready.set()
                # Let the websockets context close connections before awaiting
                # asyncio.Server.wait_closed (which also waits for clients on 3.12).
                await asyncio.Future()
        finally:
            self.service.request_chat_shutdown()
            await self.dispatcher.aclose(cancel=True)
            self.service.remove_event_listener(self.broadcast)
            await self.service.aclose()
