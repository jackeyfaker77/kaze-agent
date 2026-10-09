"""在本机接收 OpenAI 浏览器 OAuth 回调，授权码不进入 RPC 或日志。"""
from __future__ import annotations

import asyncio
import html
import secrets
from urllib.parse import parse_qs, urlsplit

from agent.model_runtime.auth.codex import BrowserLogin, CodexAuthDriver
from agent.model_runtime.errors import AuthenticationError

CODEX_CALLBACK_PORT = 1455
_CALLBACK_PATH = "/auth/callback"
_MAX_HEADER_BYTES = 8192


class CodexBrowserCallbacks:
    """同一宿主共享监听器，按每次登录的随机 state 分发回调。"""

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._clients: set[asyncio.Task] = set()

    async def begin(self, auth: CodexAuthDriver) -> tuple[BrowserLogin, asyncio.Future[str]]:
        async with self._lock:
            if self._server is None:
                try:
                    self._server = await asyncio.start_server(
                        self._accept, "127.0.0.1", CODEX_CALLBACK_PORT, limit=_MAX_HEADER_BYTES,
                    )
                except OSError as exc:
                    raise AuthenticationError(
                        "无法接收浏览器授权：本地端口 1455 不可用。请关闭其他正在登录的 Codex 客户端后重试。"
                    ) from exc
            port = self._server.sockets[0].getsockname()[1]
            login = auth.begin_browser_login(f"http://localhost:{port}{_CALLBACK_PATH}")
            result = asyncio.get_running_loop().create_future()
            self._pending[login.state] = result
            return login, result

    async def release(self, state: str) -> None:
        async with self._lock:
            result = self._pending.pop(state, None)
            if result is not None and not result.done():
                result.cancel()
            if not self._pending and self._server is not None:
                server, self._server = self._server, None
                server.close()
                await server.wait_closed()

    async def aclose(self) -> None:
        async with self._lock:
            for result in self._pending.values():
                if not result.done():
                    result.cancel()
            self._pending.clear()
            if self._server is not None:
                server, self._server = self._server, None
                server.close()
                await server.wait_closed()
        clients = list(self._clients)
        for client in clients:
            client.cancel()
        await asyncio.gather(*clients, return_exceptions=True)

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self._clients.add(task)
        task.add_done_callback(self._clients.discard)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(10):
                raw = await reader.readuntil(b"\r\n\r\n")
                if len(raw) > _MAX_HEADER_BYTES:
                    raise ValueError("oversized headers")
                lines = raw.decode("ascii").split("\r\n")
                method, target, version = lines[0].split()
                if method != "GET" or version not in {"HTTP/1.0", "HTTP/1.1"}:
                    raise ValueError("unsupported request")
                hosts = [line.split(":", 1)[1].strip().lower() for line in lines[1:] if line.lower().startswith("host:")]
                port = writer.get_extra_info("sockname")[1]
                if len(hosts) != 1 or hosts[0] not in {f"localhost:{port}", f"127.0.0.1:{port}"}:
                    raise ValueError("unexpected host")
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != _CALLBACK_PATH:
                    await self._reply(writer, 404, "页面不存在，请回到 Kaze 完成登录。")
                    return
                params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
                states = params.get("state", [])
                if len(states) != 1 or not states[0].isascii():
                    raise ValueError("invalid state")
                key = next((key for key in self._pending if secrets.compare_digest(key, states[0])), None)
                result = self._pending.get(key) if key is not None else None
                if result is None or result.done():
                    await self._reply(writer, 400, "登录请求已失效，请回到 Kaze 重新登录。")
                    return
                codes, errors = params.get("code", []), params.get("error", [])
                if errors and not codes and len(errors) == 1 and errors[0]:
                    message = "你已取消 OpenAI 授权，请重新登录。" if errors[0] == "access_denied" else "OpenAI 未完成登录授权，请重新登录。"
                    result.set_exception(AuthenticationError(message))
                    await self._reply(writer, 400, message)
                    return
                if len(codes) != 1 or not codes[0] or errors:
                    raise ValueError("invalid authorization result")
                result.set_result(codes[0])
                await self._reply(writer, 200, "已收到 OpenAI 授权。请回到 Kaze，连接状态会自动更新。")
        except (ValueError, UnicodeDecodeError, asyncio.LimitOverrunError):
            await self._reply(writer, 400, "授权回调无效，请回到 Kaze 重新登录。")
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, message: str) -> None:
        body = (
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Kaze · ChatGPT 登录</title><body><h1>Kaze · ChatGPT 登录</h1>'
            f'<p>{html.escape(message)}</p></body></html>'
        ).encode("utf-8")
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}[status]
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Referrer-Policy: no-referrer\r\n"
            "Content-Security-Policy: default-src 'none'; base-uri 'none'; frame-ancestors 'none'\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(headers + body)
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass
