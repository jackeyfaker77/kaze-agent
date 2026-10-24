"""
入口

正式模式（从仓库根目录执行）：
  uv run python apps/backend/main.py          启动桌面 bridge 主链
  uv run python apps/backend/main.py bridge   启动桌面 bridge 主链
  uv run python apps/backend/main.py host     启动独立常驻后端，不依赖桌面或 stdin
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import json
import sys
from pathlib import Path

from agent.config import Config
from bootstrap.app import (
    DESKTOP_RUNTIME_FEATURES,
    build_app_runtime,
    configure_logging_stream,
)
from bootstrap.init_workspace import InitSummary, init_workspace
from core.common.workspace import resolve_default_workspace
from core.net.http import SharedHttpResources
from bootstrap.host_state import WorkspaceInUse, host_status, request_host_stop
from bootstrap.runtime_host import Attachment, RuntimeHost

logger = logging.getLogger(__name__)


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动 Shiori runtime")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("bridge", "desktop", "host", "host-status", "host-stop", "conversation-rebuild", "setup", "init"),
        help="运行命令；desktop 是 bridge 的兼容别名",
    )
    parser.add_argument("--config", default="config.toml", help="配置文件路径")
    parser.add_argument("--workspace", type=Path, help="工作区路径")
    parser.add_argument("--remote-port", type=int, help="Host 的本机 WebSocket 端口；通过 TLS 代理或 SSH 隧道连接")
    parser.add_argument("--remote-token-file", type=Path, help="远程连接认证 token 文件（至少 32 字符）")
    parser.add_argument("--force", action="store_true", help="初始化时覆盖现有文件")
    parser.add_argument(
        "--inspect-modules",
        action="store_true",
        help="输出模块检查结果后退出",
    )
    return parser


def _log_init_summary(summary: InitSummary) -> None:
    def _log_group(title: str, paths: list[Path]) -> None:
        if not paths:
            return
        logger.info("%s", title)
        for path in paths:
            logger.info("  %s", path)

    _log_group("已创建：", summary.created)
    _log_group("已覆盖：", summary.overwritten)
    _log_group("已跳过：", summary.skipped)
    if summary.notes:
        logger.info("说明：")
        for note in summary.notes:
            logger.info("  %s", note)
    if summary.next_steps:
        logger.info("下一步：")
        for step in summary.next_steps:
            logger.info("  %s", step)


async def inspect_modules(
    config_path: str = "config.toml",
    workspace: Path | None = None,
) -> None:
    import logging
    from bootstrap.tools import build_core_runtime

    logging.getLogger().setLevel(logging.WARNING)
    config = Config.load(config_path)
    http_resources = SharedHttpResources()
    runtime = build_core_runtime(
        config,
        workspace or resolve_default_workspace(),
        http_resources,
    )
    try:
        print(await runtime.inspect_modules())
    finally:
        await runtime.stop()
        await http_resources.aclose()


async def serve_bridge(
    config_path: str = "config.toml",
    workspace: Path | None = None,
) -> None:
    configure_logging_stream(sys.stderr)
    runtime = build_app_runtime(
        Config.load(config_path),
        workspace=workspace or resolve_default_workspace(),
        features=DESKTOP_RUNTIME_FEATURES,
    )
    async def attach(core_runtime):
        from desktop_bridge import DesktopBridgeServer
        server = DesktopBridgeServer(core_runtime)
        await server.serve_stdio()
    await RuntimeHost(runtime, mode="bridge").run(attach)


async def serve_host(config_path: str, workspace: Path | None = None, *, remote_port: int | None = None, remote_token_file: Path | None = None) -> None:
    attachment: Attachment | None = None
    if remote_port is not None:
        from bootstrap.remote_host import RemoteHostServer, read_remote_token
        if remote_token_file is None:
            raise ValueError("--remote-port requires --remote-token-file")
        token = read_remote_token(remote_token_file)
        async def serve_remote(core):
            await RemoteHostServer(core, token, port=remote_port).run()
        attachment = serve_remote
    runtime = build_app_runtime(Config.load(config_path), workspace=workspace or resolve_default_workspace())
    await RuntimeHost(runtime).run(attachment, handle_signals=True)


def main(argv: list[str] | None = None) -> int:
    """解析命令行参数并执行对应的 runtime 入口。"""
    configure_logging_stream(sys.stderr)
    parser = _build_argument_parser()
    args = parser.parse_args(argv)
    if args.remote_port is not None or args.remote_token_file is not None:
        if args.command != "host" or args.remote_port is None or args.remote_token_file is None:
            parser.error("远程监听参数只能用于 host，且端口与 token 文件必须同时提供")
        if not 1 <= args.remote_port <= 65535:
            parser.error("远程端口必须在 1–65535 之间")
    config_path = str(args.config)
    workspace = args.workspace

    if args.command == 'conversation-rebuild':
        from bootstrap.host_state import WorkspaceOwner
        from session.store import SessionStore
        target = (workspace or resolve_default_workspace()).resolve()
        if not (target / 'private_runtime/execution/events.sqlite3').is_file():
            raise ValueError('没有可用于重建的 Runtime Event Log；请先完成旧历史迁移')
        try:
            owner = WorkspaceOwner(target, 'maintenance')
        except WorkspaceInUse as exc:
            logger.error('%s', exc)
            return 2
        try:
            store = SessionStore(target / 'sessions.db')
            try:
                store.rebuild_projection()
                print(json.dumps({'rebuilt': True, 'sessions': len(store.list_sessions())}))
            finally:
                store.close()
        finally:
            owner.close()
        return 0

    if args.command in {"host-status", "host-stop"}:
        target = workspace or resolve_default_workspace()
        result = host_status(target) if args.command == "host-status" else request_host_stop(target)
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.command == "setup":
        from bootstrap.setup_wizard import run_setup_wizard

        run_setup_wizard(
            config_path=Path(config_path),
            workspace=workspace or resolve_default_workspace(),
        )
        return 0

    if args.command == "init":
        summary = init_workspace(
            config_path=config_path,
            workspace=workspace or resolve_default_workspace(),
            force=args.force,
        )
        _log_init_summary(summary)
        return 0

    if not Path(config_path).exists():
        logger.error(
            f"找不到配置文件 {config_path!r}，请先复制 config/examples/config.example.toml 为 config.toml。"
        )
        return 1

    try:
        if args.inspect_modules:
            asyncio.run(inspect_modules(config_path, workspace))
        elif args.command == "host":
            if args.remote_port is None:
                asyncio.run(serve_host(config_path, workspace))
            else:
                asyncio.run(serve_host(config_path, workspace, remote_port=args.remote_port, remote_token_file=args.remote_token_file))
        else:
            asyncio.run(serve_bridge(config_path, workspace))
    except WorkspaceInUse as exc:
        logger.error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
