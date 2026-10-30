"""McpServerRegistry: 管理多个 MCP server 连接，持久化到 mcp_servers.json。"""

import asyncio
import json
import logging
from contextvars import Context
from pathlib import Path
from typing import Any

from agent.mcp.client import McpClient
from agent.mcp.tool import McpToolWrapper
from agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class McpServerRegistry:
    """管理 MCP server 连接生命周期，并将工具同步进 ToolRegistry。

    持久化格式（mcp_servers.json）：
    {
      "servers": {
        "calendar": {
          "command": ["python", "/path/to/run_server.py"],
          "env": {"GOOGLE_CLIENT_ID": "..."}
        }
      }
    }
    """

    def __init__(self, config_path: Path, tool_registry: ToolRegistry) -> None:
        self._config_path = config_path
        self._tool_registry = tool_registry
        self._clients: dict[str, McpClient] = {}
        self._server_tools: dict[str, list[str]] = (
            {}
        )  # server_name -> 已注册的工具名列表
        self._connect_task: asyncio.Task[None] | None = None
        self._configs: dict[str, Any] = self._load_raw_configs()
        self._locks: dict[str, asyncio.Lock] = {}
        self._recovery: dict[str, asyncio.Task[None]] = {}
        self._errors: dict[str, str] = {}
        self._closed = False
        self._retry_base = 2.0
        self._retry_cap = 60.0
        self._health_interval = 1.0

    def health(self) -> dict[str, bool]:
        return {name: name in self._clients and getattr(self._clients[name], 'connected', True)
                for name, cfg in self._configs.items() if cfg.get('enabled', True)}

    async def _drop(self, name: str) -> None:
        for tool in self._server_tools.pop(name, []):
            self._tool_registry.unregister(tool)
        client = self._clients.pop(name, None)
        if client is not None:
            await client.disconnect()

    async def _ensure(self, name: str) -> None:
        async with self._locks.setdefault(name, asyncio.Lock()):
            cfg = self._configs.get(name)
            if self._closed or not cfg or not cfg.get('enabled', True):
                return
            if self.health().get(name):
                return
            await self._drop(name)
            await self._connect(name, cfg['command'], cfg.get('env'), cfg.get('cwd'))
            self._errors.pop(name, None)

    def _watch(self, name: str) -> None:
        if not self._closed and (name not in self._recovery or self._recovery[name].done()):
            self._recovery[name] = asyncio.create_task(self._recover(name), name=f'mcp_recovery:{name}', context=Context())

    async def _recover(self, name: str) -> None:
        delay = self._retry_base
        while not self._closed and name in self._configs:
            await asyncio.sleep(self._health_interval if self.health().get(name) else delay)
            try:
                await self._ensure(name)
                delay = self._retry_base
            except Exception as exc:
                self._errors[name] = type(exc).__name__
                logger.warning('[mcp] recovery %r failed: %s; retry in %.1fs', name, type(exc).__name__, min(delay * 2, self._retry_cap))
                delay = min(delay * 2, self._retry_cap)

    async def load_and_connect_all(self) -> None:
        """启动时读取持久化配置，重连所有 server。"""
        async def connect_one(name: str, cfg: dict[str, Any]) -> None:
            if not cfg.get('enabled', True):
                return
            try:
                await self._ensure(name)
            except Exception as e:
                self._errors[name] = type(e).__name__
                logger.error("[mcp] 重连 %r 失败: %s", name, type(e).__name__)
            self._watch(name)

        self._configs = self._load_raw_configs()
        for name in list(self._recovery.keys() | self._clients.keys()):
            if name not in self._configs or not self._configs[name].get('enabled', True):
                task = self._recovery.pop(name, None)
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                async with self._locks.setdefault(name, asyncio.Lock()):
                    await self._drop(name)
        await asyncio.gather(
            *(
                connect_one(name, cfg)
                for name, cfg in list(self._configs.items())
            )
        )

    def start_connect_all_background(self) -> None:
        """后台重连所有 server，不阻塞主服务启动。"""
        if self._connect_task is None or self._connect_task.done():
            self._connect_task = asyncio.create_task(
                self.load_and_connect_all(),
                name="mcp_connect_all",
            )

    async def connect_declared_servers(self, specs) -> None:
        """接通插件声明的 MCP，保持与普通 Agent 共用连接及工具目录。"""
        if self._connect_task is not None:
            await self._connect_task
        for spec in specs:
            if spec.name in self._clients:
                client = self._clients[spec.name]
                if client.command != list(spec.command) or client.env != spec.env:
                    raise ValueError(f"插件 MCP 与已连接的 server 配置冲突: {spec.name}")
                if spec.cwd is not None and Path(client.cwd or ".").resolve() != Path(spec.cwd).resolve():
                    raise ValueError(f"插件 MCP 与已连接的 server 工作目录冲突: {spec.name}")
                continue
            cfg = {'command': list(spec.command), 'env': spec.env, 'cwd': spec.cwd}
            existing = self._configs.get(spec.name)
            if existing is not None and (existing.get('command') != cfg['command'] or (existing.get('env') or {}) != (cfg['env'] or {})):
                raise ValueError(f'插件 MCP 与配置冲突: {spec.name}')
            if existing is not None and spec.cwd is not None and Path(existing.get('cwd') or '.').resolve() != Path(spec.cwd).resolve():
                raise ValueError(f'插件 MCP 与配置工作目录冲突: {spec.name}')
            self._configs.setdefault(spec.name, cfg)
            try:
                await self._ensure(spec.name)
            except Exception as exc:
                self._errors[spec.name] = type(exc).__name__
            self._watch(spec.name)

    async def shutdown(self) -> None:
        self._closed = True
        if self._connect_task is not None and not self._connect_task.done():
            self._connect_task.cancel()
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass
        for task in self._recovery.values():
            task.cancel()
        await asyncio.gather(*self._recovery.values(), return_exceptions=True)
        self._recovery.clear()
        for names in self._server_tools.values():
            for name in names:
                self._tool_registry.unregister(name)
        clients = list(self._clients.values())
        self._clients.clear()
        self._server_tools.clear()
        await asyncio.gather(
            *(client.disconnect() for client in clients),
            return_exceptions=True,
        )

    async def add(
        self,
        name: str,
        command: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> str:
        async with self._locks.setdefault(name, asyncio.Lock()):
            if self._closed:
                return 'MCP registry 已关闭。'
            return await self._add(name, command, env, cwd)

    async def _add(self, name, command, env, cwd) -> str:
        if name in self._configs or name in self._clients:
            return f"MCP server {name!r} 已存在。如需更新，请先 mcp_remove 再重新添加。"
        try:
            tool_names = await self._connect(name, command, env, cwd)
        except Exception as e:
            return f"连接 MCP server {name!r} 失败：{e}"
        self._configs[name] = {'command': command, 'env': env, 'cwd': cwd}
        self._save()
        self._watch(name)
        return (
            f"已连接 MCP server {name!r}，注册了 {len(tool_names)} 个工具：\n"
            + "\n".join(f"- {n}" for n in tool_names)
        )

    async def remove(self, name: str) -> str:
        if name not in self._configs and name not in self._clients:
            return f"MCP server {name!r} 不存在，当前已注册：{list(self._clients.keys()) or '无'}"
        task = self._recovery.pop(name, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._locks.setdefault(name, asyncio.Lock()):
            self._configs.pop(name, None)
            await self._drop(name)
            self._save()
        return f"已注销 MCP server {name!r}。"

    def list_servers(self) -> str:
        if not self._configs:
            return "当前没有已注册的 MCP server。"
        lines = []
        for name in self._configs:
            tools = self._server_tools.get(name, [])
            state = '' if self.health().get(name) else f" [离线／恢复中: {self._errors.get(name, 'not_connected')}]"
            lines.append(f"- {name}（{len(tools)} 个工具）：{', '.join(tools) or '无'}{state}")
        return "\n".join(lines)

    async def _connect(
        self,
        name: str,
        command: list[str],
        env: dict[str, str] | None,
        cwd: str | None = None,
    ) -> list[str]:
        client = McpClient(name=name, command=command, env=env, cwd=cwd)
        tool_names = []
        try:
            tool_infos = await client.connect()
            if self._closed:
                raise RuntimeError('MCP registry closed during connect')
            for info in tool_infos:
                wrapper = McpToolWrapper(client, info)
                self._tool_registry.register(
                    wrapper,
                    risk="external-side-effect",
                    source_type="mcp",
                    source_name=name,
                )
                tool_names.append(wrapper.name)
        except BaseException:
            for tool_name in tool_names:
                self._tool_registry.unregister(tool_name)
            await client.disconnect()
            raise
        self._clients[name] = client
        self._server_tools[name] = tool_names
        return tool_names

    def _load_raw_configs(self) -> dict[str, Any]:
        if not self._config_path.exists():
            return {}
        try:
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
            return data.get("servers", {})
        except Exception as e:
            logger.warning("[mcp] 读取配置失败 %s: %s", self._config_path, e)
            return {}

    def _save(self) -> None:
        servers = self._configs
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            self._config_path.write_text(
                json.dumps({"servers": servers}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("[mcp] 保存配置失败: %s", e)
