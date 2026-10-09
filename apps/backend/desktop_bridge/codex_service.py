"""提供浏览器回调登录和模型目录；RPC 结果不包含 OAuth token。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

from agent.model_runtime.auth.codex import BrowserLogin, CodexAuthDriver
from agent.model_runtime.auth.store import workspace_store
from agent.model_runtime.catalog.codex import CodexModelCatalog
from agent.model_runtime.errors import ModelRuntimeError

from .codex_oauth import CodexBrowserCallbacks

_LOGIN_TIMEOUT_SECONDS = 900


class CodexConnectionService:
    def __init__(self, workspace: Path):
        self.store = workspace_store(workspace)
        self.logins: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.browser_logins: dict[str, BrowserLogin] = {}
        self.callbacks = CodexBrowserCallbacks()
        self._login_locks: dict[str, asyncio.Lock] = {}

    async def handle(self, method: str, payload: dict) -> dict:
        connection_id = str(UUID(str(payload.get("connection_id") or "")))
        auth = CodexAuthDriver(self.store, connection_id)
        if method == "codex.status":
            metadata = await asyncio.to_thread(self.store.metadata)
            return {"configured": metadata.get(connection_id, {}).get("driver") == "codex"}
        if method == "codex.models":
            try:
                async with asyncio.timeout(90):
                    models = await CodexModelCatalog(auth).list_models()
            except ModelRuntimeError:
                raise
            except Exception as exc:
                raise ValueError("无法读取 Codex 模型，请检查网络后重试") from exc
            return {"models": [{"id": model.slug,
                                "efforts": list(model.capabilities.supported_reasoning_efforts)} for model in models]}
        if method.startswith("codex.login."):
            # Bridge 并发处理 RPC，开始和取消必须按连接串行，才能共享同一授权任务。
            async with self._login_locks.setdefault(connection_id, asyncio.Lock()):
                return await self._handle_login(method, payload, auth)
        raise ValueError("未知 Codex 请求")

    async def _handle_login(self, method: str, payload: dict, auth: CodexAuthDriver) -> dict:
        connection_id = auth.credential_id
        if method == "codex.login.start":
            previous = self.tasks.get(connection_id)
            if previous and not previous.done():
                return dict(self.logins[connection_id])
            try:
                login, callback = await self.callbacks.begin(auth)
            except ModelRuntimeError:
                raise
            except Exception as exc:
                raise ValueError("无法开始 Codex 登录，请检查网络后重试") from exc
            state = {"login_id": uuid4().hex, "status": "waiting",
                     "authorization_url": login.authorization_url, "interval": 1}
            self.logins[connection_id] = state
            self.browser_logins[connection_id] = login
            self.tasks[connection_id] = asyncio.create_task(self._complete(auth, login, callback, state))
            return dict(state)
        if method in {"codex.login.status", "codex.login.cancel"}:
            state = self.logins.get(connection_id)
            if not state or payload.get("login_id") != state["login_id"]:
                raise ValueError("登录请求已失效，请重新开始")
            if method == "codex.login.cancel" and state["status"] == "waiting":
                task = self.tasks.get(connection_id)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                # Task 可能在首次执行前就取消，此时协程的 finally 尚未进入。
                login = self.browser_logins.pop(connection_id, None)
                if login is not None:
                    await self.callbacks.release(login.state)
                state["status"] = "cancelled"
            return dict(state)
        raise ValueError("未知 Codex 请求")

    async def _complete(self, auth, login, callback, state):
        """等待本机浏览器回调；取消后不提交新的登录凭据。"""
        try:
            async with asyncio.timeout(_LOGIN_TIMEOUT_SECONDS):
                authorization_code = await callback
                credential = await asyncio.to_thread(auth.complete_browser_login, login, authorization_code)
                # 短暂的原子本地提交不跨 await，避免取消与持久化产生竞态。
                auth.store.put(auth.credential_id, credential)
                state["status"] = "connected"
        except asyncio.CancelledError:
            state["status"] = "cancelled"
            raise
        except TimeoutError:
            state.update(status="failed", error="授权已超时，请重新登录")
        except ModelRuntimeError as exc:
            state.update(status="failed", error=str(exc))
        except Exception:
            state.update(status="failed", error="Codex 登录未完成，请检查网络后重试")
        finally:
            if self.browser_logins.get(auth.credential_id) is login:
                self.browser_logins.pop(auth.credential_id, None)
            await self.callbacks.release(login.state)

    async def aclose(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        await self.callbacks.aclose()
        self.browser_logins.clear()
