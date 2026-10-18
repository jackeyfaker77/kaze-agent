"""Prepare an independent MCP environment and merge optional workspace wiring."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import venv


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_object(path: Path, default: dict) -> dict:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a JSON object")
    return data


def source_config(workspace: Path) -> dict:
    data = read_object(workspace / "proactive_sources.json", {"sources": []})
    rows = data.get("sources")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("proactive_sources.json must contain a list of source objects")
    matches = [row for row in rows if row.get("server") == "calendar"]
    if matches:
        if len(matches) != 1:
            raise ValueError(
                "Multiple calendar source routes exist; resolve the conflict"
            )
        row = matches[0]
        if (
            row.get("channel") != "alert"
            or row.get("get_tool", row.get("fetch_tool")) != "get_proactive_events"
            or row.get("ack_tool") != "acknowledge_events"
            or row.get("validate_tool") != "validate_proactive_events"
            or not isinstance(row.get("get_args", {}), dict)
            or not isinstance(row.get("ack_args", {}), dict)
        ):
            raise ValueError(
                "An incompatible calendar source exists; preserving its configuration"
            )
        row["enabled"] = True
    else:
        rows.append(
            {
                "server": "calendar",
                "channel": "alert",
                "get_tool": "get_proactive_events",
                "ack_tool": "acknowledge_events",
                "validate_tool": "validate_proactive_events",
                "enabled": True,
            }
        )
    return data


def server_config(workspace: Path, specification: dict) -> dict:
    data = read_object(workspace / "mcp_servers.json", {"servers": {}})
    servers = data.get("servers")
    if not isinstance(servers, dict):
        raise ValueError("mcp_servers.json must contain a servers object")
    desired = {key: value for key, value in specification.items() if key != "name"}
    if "calendar" in servers and servers["calendar"] != desired:
        raise ValueError(
            "A different calendar MCP already exists; preserving its configuration"
        )
    servers["calendar"] = desired
    return data


def skill_content(root: Path, workspace: Path) -> bytes:
    source = root / "skills/calendar-todo/SKILL.md"
    destination = workspace / "skills/calendar-todo/SKILL.md"
    marker = destination.parent / ".kaze-calendar-managed"
    content = source.read_bytes()
    if destination.exists() and destination.read_bytes() != content:
        previous = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
        if previous != fingerprint(destination.read_bytes()):
            raise ValueError(
                "A customized calendar-todo skill exists; preserve it with --skip-skill"
            )
    return content


def fingerprint(value: bytes) -> str:
    return sha256(value.replace(b"\r\n", b"\n")).hexdigest()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--config",
        type=Path,
        help="Existing JSON calendar settings; defaults to workspace/mcp-data/kaze-calendar/config.json",
    )
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--skip-skill", action="store_true")
    parser.add_argument("--enable-proactive", action="store_true")
    parser.add_argument(
        "--register",
        action="store_true",
        help="Persist the MCP registration for the next backend start",
    )
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error("Python 3.12 or later is required")
    root = Path(__file__).resolve().parents[1]
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        parser.error("--workspace must point to Kaze's existing workspace")
    python = (
        root
        / ".venv"
        / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    config_path = (
        args.config.expanduser().resolve()
        if args.config
        else workspace / "mcp-data/kaze-calendar/config.json"
    )
    specification = {
        "name": "calendar",
        "command": [str(python), str(root / "run_mcp.py")],
        "env": {"KAZE_CALENDAR_CONFIG": str(config_path)},
        "cwd": str(root),
    }
    # Validate every existing setting before installing or changing workspace files.
    sys.path.insert(0, str(root / "src"))
    from kaze_calendar.config import Settings

    try:
        if config_path.exists():
            Settings.load(config_path)
        elif args.config:
            raise ValueError("--config must point to an existing settings file")
        sources = source_config(workspace) if args.enable_proactive else None
        servers = server_config(workspace, specification) if args.register else None
        content = skill_content(root, workspace) if not args.skip_skill else None
    except (ValueError, TypeError, OSError) as exc:
        parser.error(str(exc))
    if args.skip_install and not python.is_file():
        parser.error("--skip-install requires a prepared .venv")
    if not args.skip_install:
        if not python.is_file():
            venv.EnvBuilder(with_pip=True).create(root / ".venv")
        subprocess.run(
            [str(python), "-m", "pip", "install", "."],
            cwd=root,
            check=True,
            stdout=sys.stderr,
        )
    # Dependency installation may take time; recheck files before merging current state.
    try:
        if config_path.exists():
            Settings.load(config_path)
        sources = source_config(workspace) if args.enable_proactive else None
        servers = server_config(workspace, specification) if args.register else None
        content = skill_content(root, workspace) if not args.skip_skill else None
    except (ValueError, TypeError, OSError) as exc:
        parser.error(str(exc))
    if not config_path.exists():
        atomic_text(
            config_path, (root / "config.example.json").read_text(encoding="utf-8")
        )
    if content is not None:
        destination = workspace / "skills/calendar-todo/SKILL.md"
        atomic_text(destination, content.decode("utf-8"))
        atomic_text(
            destination.parent / ".kaze-calendar-managed", fingerprint(content) + "\n"
        )
    for name, data in (
        ("mcp_servers.json", servers),
        ("proactive_sources.json", sources),
    ):
        if data is not None:
            atomic_text(
                workspace / name, json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            )
    print(json.dumps(specification, ensure_ascii=False, indent=2))
    if args.register:
        print(
            "MCP registration saved; restart Kaze's backend to load it.",
            file=sys.stderr,
        )
    else:
        print(
            "Pass this JSON to Kaze's mcp_add to register the service.", file=sys.stderr
        )
    if args.enable_proactive:
        print(
            "Calendar reminder source declared; global proactive settings still apply.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
