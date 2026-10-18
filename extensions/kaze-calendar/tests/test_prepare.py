import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "calendar_prepare", Path(__file__).parents[1] / "scripts/prepare.py"
)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def test_source_and_registration_merge_preserve_existing_configuration(tmp_path):
    feed = {"server": "feed", "channel": "content", "get_tool": "events"}
    path = tmp_path / "proactive_sources.json"
    path.write_text(json.dumps({"sources": [feed], "note": "保留"}), encoding="utf-8")
    merged = prepare.source_config(tmp_path)
    assert merged["sources"][0] == feed and merged["note"] == "保留"
    row = merged["sources"][1]
    assert (
        row["channel"] == "alert"
        and row["validate_tool"] == "validate_proactive_events"
    )
    prepare.atomic_text(path, json.dumps(merged))
    assert prepare.source_config(tmp_path) == merged
    path = tmp_path / "mcp_servers.json"
    path.write_text(
        json.dumps({"servers": {"feed": {"command": ["old"]}}}), encoding="utf-8"
    )
    declaration = {"name": "calendar", "command": ["python", "run_mcp.py"]}
    assert prepare.server_config(tmp_path, declaration)["servers"]["feed"][
        "command"
    ] == ["old"]
    merged = prepare.server_config(tmp_path, declaration)
    prepare.atomic_text(path, json.dumps(merged))
    with pytest.raises(ValueError):
        prepare.server_config(tmp_path, {**declaration, "command": ["other"]})


def test_incompatible_calendar_route_is_preserved(tmp_path):
    path = tmp_path / "proactive_sources.json"
    before = json.dumps(
        {
            "sources": [
                {"server": "calendar", "channel": "content", "get_tool": "custom"}
            ]
        }
    )
    path.write_text(before, encoding="utf-8")
    with pytest.raises(ValueError):
        prepare.source_config(tmp_path)
    assert path.read_text(encoding="utf-8") == before


def test_skill_upgrade_preserves_user_changes(tmp_path):
    root = Path(__file__).parents[1]
    content = prepare.skill_content(root, tmp_path)
    target = tmp_path / "skills/calendar-todo/SKILL.md"
    prepare.atomic_text(target, content.decode("utf-8"))
    prepare.atomic_text(
        target.parent / ".kaze-calendar-managed", prepare.fingerprint(content)
    )
    assert prepare.skill_content(root, tmp_path) == content
    target.write_text("用户修改", encoding="utf-8")
    with pytest.raises(ValueError, match="customized"):
        prepare.skill_content(root, tmp_path)
