import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "device_prepare", Path(__file__).parents[1] / "scripts/prepare.py"
)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def test_source_merge_preserves_feed_and_other_metadata(tmp_path):
    feed = {
        "server": "feed",
        "channel": "content",
        "get_tool": "get_proactive_events",
        "enabled": True,
    }
    path = tmp_path / "proactive_sources.json"
    path.write_text(json.dumps({"sources": [feed], "note": "保留"}), encoding="utf-8")
    result = prepare.source_config(tmp_path)
    assert result["note"] == "保留"
    assert result["sources"][0] == feed
    assert result["sources"][1]["get_tool"] == "get_context"
    assert "ack_tool" not in result["sources"][1]
    assert json.loads(path.read_text(encoding="utf-8"))["sources"] == [feed]
    prepare.atomic_text(path, json.dumps(result))
    assert prepare.source_config(tmp_path) == result


@pytest.mark.parametrize(
    "row",
    [
        {"server": "device", "channel": "alert", "get_tool": "get_context"},
        {"server": "device", "channel": "context", "get_tool": "custom"},
        {
            "server": "device",
            "channel": "context",
            "get_tool": "get_context",
            "ack_tool": "ack",
        },
        {
            "server": "device",
            "channel": "context",
            "get_tool": "get_context",
            "get_args": [],
        },
        {
            "server": "device",
            "channel": "context",
            "get_tool": "get_context",
            "get_args": None,
        },
    ],
)
def test_conflicting_device_source_is_not_overwritten(tmp_path, row):
    path = tmp_path / "proactive_sources.json"
    before = json.dumps({"sources": [row]})
    path.write_text(before, encoding="utf-8")
    with pytest.raises(ValueError, match="incompatible"):
        prepare.source_config(tmp_path)
    assert path.read_text(encoding="utf-8") == before


def test_registration_preserves_existing_servers_and_rejects_conflicts(tmp_path):
    path = tmp_path / "mcp_servers.json"
    path.write_text(
        json.dumps({"servers": {"feed": {"command": ["feed"]}}, "extra": 1}),
        encoding="utf-8",
    )
    declaration = {
        "name": "device",
        "command": ["python", "device.py"],
        "env": {},
        "cwd": ".",
    }
    result = prepare.server_config(tmp_path, declaration)
    assert result["servers"]["feed"] == {"command": ["feed"]}
    assert result["extra"] == 1
    prepare.atomic_text(path, json.dumps(result))
    assert prepare.server_config(tmp_path, declaration) == result
    before = path.read_bytes()
    with pytest.raises(ValueError, match="different"):
        prepare.server_config(tmp_path, {**declaration, "command": ["other"]})
    assert path.read_bytes() == before


def test_managed_skill_can_upgrade_but_customized_skill_is_preserved(tmp_path):
    root, workspace = tmp_path / "service", tmp_path / "workspace"
    source = root / "skills/device-context/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("First version", encoding="utf-8")
    content = prepare.skill_content(root, workspace)
    destination = workspace / "skills/device-context/SKILL.md"
    prepare.atomic_text(destination, content.decode())
    prepare.atomic_text(
        destination.parent / ".kaze-device-managed", prepare.fingerprint(content)
    )
    source.write_text("New version", encoding="utf-8")
    assert prepare.skill_content(root, workspace) == b"New version"
    destination.write_text("User customization", encoding="utf-8")
    with pytest.raises(ValueError, match="customized"):
        prepare.skill_content(root, workspace)


def test_windows_crlf_skill_is_installed_without_double_translation(tmp_path):
    root, workspace = tmp_path / "service", tmp_path / "workspace"
    source = root / "skills/device-context/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"First version\r\nSecond line\r\n")
    content = prepare.skill_content(root, workspace)
    destination = workspace / "skills/device-context/SKILL.md"
    prepare.atomic_text(destination, content.decode())
    prepare.atomic_text(
        destination.parent / ".kaze-device-managed", prepare.fingerprint(content)
    )
    assert destination.read_bytes() == content
    source.write_bytes(b"Updated version\r\nSecond line\r\n")
    assert prepare.skill_content(root, workspace) == source.read_bytes()
