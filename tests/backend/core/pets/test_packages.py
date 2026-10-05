from __future__ import annotations

import json
import zipfile

import pytest

from core.pets.packages import PetPackageService


def _archive(tmp_path, package_id="pet", *, wrapper="", actions=None, sprite_path="spritesheet.webp"):
    path = tmp_path / f"{package_id}.zip"
    manifest = {"id": package_id, "displayName": "桌宠", "description": "fixture",
                "spritesheetPath": sprite_path, "actions": actions or {}}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(wrapper + "pet.json", json.dumps(manifest))
        archive.writestr(wrapper + sprite_path, b"fixture")
    return path


@pytest.fixture
def packages(tmp_path, monkeypatch):
    service = PetPackageService(tmp_path / "workspace")
    monkeypatch.setattr(service, "_validate_atlas", lambda data: None)
    return service


@pytest.mark.parametrize("wrapper", ["", "pet/"])
def test_import_accepts_flat_and_wrapped_packages(tmp_path, packages, wrapper):
    result = packages.import_package(_archive(tmp_path, wrapper=wrapper, actions={"greeting": "waving"}))
    assert result["selected_package_id"] == "pet"
    assert result["packages"][0]["actions"] == {"greeting": "waving"}
    assert (packages.root / "pet" / "spritesheet.webp").read_bytes() == b"fixture"
    assert json.loads((packages.root / "pet" / "pet.json").read_text(encoding="utf-8"))["displayName"] == "桌宠"


@pytest.mark.parametrize("state", ["not-a-sprite-state", "failed"])
def test_import_rejects_unknown_and_system_action_states(tmp_path, packages, state):
    with pytest.raises(ValueError, match="动作状态无效"):
        packages.import_package(_archive(tmp_path, actions={"greeting": state}))
    assert packages.snapshot()["packages"] == []


def test_import_without_preview_or_actions(tmp_path, packages):
    result = packages.import_package(_archive(tmp_path))
    assert result["packages"][0]["actions"] == {}


def test_selection_persists_across_service_instances(tmp_path, packages):
    for name in ("idle", "wave"):
        packages.import_package(_archive(tmp_path, name))
    packages.select("idle")
    reopened = PetPackageService(tmp_path / "workspace")
    assert reopened.snapshot()["selected_package_id"] == "idle"
    with pytest.raises(ValueError, match="不存在"):
        reopened.select("missing")
    assert reopened.snapshot()["selected_package_id"] == "idle"


def test_duplicate_import_preserves_installed_package(tmp_path, packages):
    path = _archive(tmp_path)
    expected = packages.import_package(path)
    with pytest.raises(ValueError, match="已存在"):
        packages.import_package(path)
    assert packages.snapshot() == expected


def test_import_rejects_path_traversal(tmp_path, packages):
    with pytest.raises(ValueError, match="路径不安全"):
        packages.import_package(_archive(tmp_path, sprite_path="../outside.webp"))
    assert not (tmp_path / "outside.webp").exists()
