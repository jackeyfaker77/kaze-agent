"""Role-owned Codex sprite package validation and import."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from PIL import Image


_FORMAT = "codex-sprite@1"
_ATLAS_SIZE = (1536, 1872)
_CELL_SIZE = (192, 208)
_USED_CELLS = (6, 8, 8, 4, 5, 8, 6, 6, 6)
_ACTION_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ACTION_STATES = frozenset(
    {
        "idle",
        "running-right",
        "running-left",
        "waving",
        "jumping",
    }
)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _safe_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or path == ".":
        raise ValueError("桌宠包路径不安全")
    return path.as_posix()


class PetPackageService:
    """Application-wide sprite packages, with no character or conversation owner."""
    def __init__(self, workspace: Path):
        self.root = workspace / "pets"
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.root / "settings.json"

    def snapshot(self):
        settings = json.loads(self.settings_path.read_text(encoding="utf-8")) if self.settings_path.exists() else {}
        packages = []
        for manifest_path in sorted(self.root.glob("*/pet.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            packages.append({"id": manifest_path.parent.name, "display_name": manifest["displayName"],
                "spritesheet_abs": str(manifest_path.parent / "spritesheet.webp"),
                "actions": manifest.get("actions", {})})
        return {"packages": packages, "selected_package_id": settings.get("selected_package_id", "")}

    def select(self, package_id):
        if not any(p["id"] == package_id for p in self.snapshot()["packages"]):
            raise ValueError("桌宠包不存在")
        temp = self.settings_path.with_suffix(".tmp")
        temp.write_text(json.dumps({"selected_package_id": package_id}), encoding="utf-8")
        os.replace(temp, self.settings_path)
        return self.snapshot()

    def import_package(self, source):
        with zipfile.ZipFile(source) as archive:
            if sum(e.file_size for e in archive.infolist()) > 64 * 1024 * 1024:
                raise ValueError("桌宠包解压内容过大")
            names, root = self._archive_names(archive)
            manifest = self._manifest(archive, root)
            package_id = str(manifest["id"])
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", package_id):
                raise ValueError("桌宠包 id 不安全")
            sprite_name = _safe_relative_path(str(manifest["spritesheetPath"]))
            if sprite_name not in names:
                raise ValueError("桌宠包缺少精灵图")
            sprite = archive.read(self._archive_entry(root, sprite_name))
            self._validate_atlas(sprite)
            manifest["actions"] = self._manifest_actions(manifest)
            destination = self.root / package_id
            if destination.exists():
                raise ValueError("桌宠包已存在")
            temporary = Path(tempfile.mkdtemp(prefix=".import-", dir=self.root))
            try:
                (temporary / "spritesheet.webp").write_bytes(sprite)
                manifest["spritesheetPath"] = "spritesheet.webp"
                (temporary / "pet.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        return self.select(package_id)

    def _archive_names(self, archive: zipfile.ZipFile) -> tuple[set[str], str]:
        names: set[str] = set()
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            name = _safe_relative_path(entry.filename)
            if name in names:
                raise ValueError("桌宠包包含重复路径")
            names.add(name)
        if "pet.json" in names:
            return names, ""
        roots = {PurePosixPath(name).parts[0] for name in names if len(PurePosixPath(name).parts) > 1}
        if len(roots) != 1:
            raise ValueError("桌宠包缺少 pet.json")
        root = next(iter(roots))
        logical_names = {
            PurePosixPath(name).relative_to(root).as_posix()
            for name in names
            if PurePosixPath(name).parts[0] == root
        }
        if "pet.json" not in logical_names:
            raise ValueError("桌宠包缺少 pet.json")
        return logical_names, root

    def _archive_entry(self, root: str, name: str) -> str:
        return f"{root}/{name}" if root else name

    def _manifest(self, archive: zipfile.ZipFile, root: str) -> dict[str, object]:
        try:
            value = json.loads(archive.read(self._archive_entry(root, "pet.json")).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("桌宠包 pet.json 无效") from error
        if not isinstance(value, dict):
            raise ValueError("桌宠包 pet.json 必须是对象")
        for field in ("id", "displayName", "description", "spritesheetPath"):
            if not isinstance(value.get(field), str) or not str(value[field]).strip():
                raise ValueError(f"桌宠包 pet.json 缺少 {field}")
        return value

    def _manifest_actions(self, manifest: dict[str, object]) -> dict[str, str]:
        raw_actions = manifest.get("actions", {})
        if raw_actions is None:
            return {}
        if not isinstance(raw_actions, dict):
            raise ValueError("桌宠包 actions 必须是对象")
        actions: dict[str, str] = {}
        for raw_name, raw_state in raw_actions.items():
            if not isinstance(raw_name, str) or not _ACTION_NAME_PATTERN.fullmatch(raw_name):
                raise ValueError("桌宠包动作名称无效")
            if not isinstance(raw_state, str) or raw_state not in _ACTION_STATES:
                raise ValueError("桌宠包动作状态无效")
            actions[raw_name] = raw_state
        return actions

    def _validate_atlas(self, data: bytes) -> None:
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "WEBP" or image.size != _ATLAS_SIZE:
                    raise ValueError("桌宠精灵图必须是 1536 x 1872 WebP")
                alpha = image.convert("RGBA").getchannel("A")
        except OSError as error:
            raise ValueError("桌宠精灵图无效") from error
        for row, count in enumerate(_USED_CELLS):
            for column in range(8):
                occupied = alpha.crop((column * _CELL_SIZE[0], row * _CELL_SIZE[1], (column + 1) * _CELL_SIZE[0], (row + 1) * _CELL_SIZE[1])).getbbox() is not None
                if column < count and not occupied:
                    raise ValueError("桌宠精灵图缺少必需动画帧")
                if column >= count and occupied:
                    raise ValueError("桌宠精灵图未使用单元必须透明")

    def _validate_preview(self, data: bytes) -> str:
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "WEBP"}:
                    raise ValueError("桌宠预览图必须是 PNG 或 WebP")
                extension = ".png" if image.format == "PNG" else ".webp"
                width, height = image.size
        except OSError as error:
            raise ValueError("桌宠预览图无效") from error
        if not 64 <= width <= 2048 or not 64 <= height <= 2048:
            raise ValueError("桌宠预览图尺寸必须在 64 到 2048 像素之间")
        return extension
