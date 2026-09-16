"""提供设备码登录和模型目录；RPC 结果不包含 OAuth token。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

from agent.model_runtime.auth.codex import CodexAuthDriver
from agent.model_runtime.auth.store import workspace_store
from agent.model_runtime.catalog.codex import CodexModelCatalog
from agent.model_runtime.errors import ModelRuntimeError


class CodexConnectionService:
    def __init__(self, workspace: Path):
        self.store = workspace_store(workspace)
        self.logins: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}

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
        if method == "codex.login.start":
            previous = self.tasks.get(connection_id)
            if previous and not previous.done():
                return self.logins[connection_id]
            try:
                code = await asyncio.to_thread(auth.begin_device_login)
            except ModelRuntimeError:
                raise
            except Exception as exc:
                raise ValueError("无法开始 Codex 登录，请检查网络后重试") from exc
            state = {"login_id": uuid4().hex, "status": "waiting", "user_code": code.user_code,
                     "verification_uri": code.verification_uri, "interval": code.interval}
            self.logins[connection_id] = state
            self.tasks[connection_id] = asyncio.create_task(self._complete(auth, code, state))
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
                state["status"] = "cancelled"
            return dict(state)
        raise ValueError("未知 Codex 请求")

    async def _complete(self, auth, code, state):
        """后台轮询不占 RPC 通道，取消后不提交新的登录凭据。"""
        try:
            async with asyncio.timeout(900):
                while True:
                    await asyncio.sleep(code.interval)
                    credential = await asyncio.to_thread(auth.poll_device_login, code)
                    if credential is not None:
                        # 短暂的原子本地提交不跨 await，避免取消与持久化产生竞态。
                        auth.store.put(auth.credential_id, credential)
                        state["status"] = "connected"
                        return
        except asyncio.CancelledError:
            state["status"] = "cancelled"
            raise
        except TimeoutError:
            state.update(status="failed", error="授权已超时，请重新登录")
        except ModelRuntimeError as exc:
            state.update(status="failed", error=str(exc))
        except Exception:
            state.update(status="failed", error="Codex 登录未完成，请检查网络后重试")

    async def aclose(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
