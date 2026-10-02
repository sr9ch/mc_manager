"""Launchers that may share one Minecraft game directory."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .discovery import MC_RE, detected, parse_version_metadata, read_json
from .models import DetectedValue, MinecraftInstance
from .paths import config_dir, data_dir, minecraft_dir, roaming_dir
from .safety import CONTENT_DIRS


def _unescape_property(value: str) -> str:
    """Decode escapes written by Java Properties.store, including Windows paths."""
    result = []
    index = 0
    escapes = {"t": "\t", "r": "\r", "n": "\n", "f": "\f"}
    while index < len(value):
        if value[index] != "\\" or index + 1 == len(value):
            result.append(value[index])
            index += 1
            continue
        next_char = value[index + 1]
        if next_char == "u" and index + 5 < len(value):
            digits = value[index + 2 : index + 6]
            if all(char in "0123456789abcdefABCDEF" for char in digits):
                result.append(chr(int(digits, 16)))
                index += 6
                continue
        result.append(escapes.get(next_char, next_char))
        index += 2
    return "".join(result)


def _plain_windows_path(value: str) -> bool:
    """Allow hand-edited paths whose backslashes were not Java-escaped."""
    return (
        len(value) >= 3
        and value[0].isalpha()
        and value[1:3] == ":\\"
        and (len(value) == 3 or value[3] != "\\")
    ) or (value.startswith("\\\\") and not value.startswith("\\\\\\\\"))


def properties(path: Path) -> dict[str, str]:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
            return {}
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    values = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue
        name = _unescape_property(key.strip()).lower()
        raw_value = value.strip()
        values[name] = (
            raw_value
            if name in GAME_KEYS and _plain_windows_path(raw_value)
            else _unescape_property(raw_value)
        )
    return values


GAME_KEYS = (
    "minecraft.gamedir",
    "minecraft.directory",
    "minecraft.dir",
    "game.directory",
    "game.dir",
    "gamedir",
    "directory",
)
VERSION_KEYS = (
    "login.version",
    "login.version.game",
    "selectedversion",
    "minecraft.version",
    "lastversion",
    "version",
)


def configured_directory(values: dict[str, str], fallback: Path) -> Path:
    raw = next((values[k] for k in GAME_KEYS if values.get(k)), None)
    if not raw:
        return fallback
    path = Path(os.path.expandvars(raw)).expanduser()
    return path if path.is_absolute() else fallback


def version_from_game(
    root: Path, selected: str | None = None
) -> tuple[DetectedValue, DetectedValue, DetectedValue]:
    versions = root / "versions"
    if versions.is_symlink() or not versions.is_dir():
        return DetectedValue(), DetectedValue(), DetectedValue()
    if selected:
        name = Path(selected).name
        if name == selected:
            result = parse_version_metadata(versions / name / f"{name}.json")
            if result[0].value:
                return result
        return DetectedValue(), DetectedValue(), DetectedValue()
    candidates = []
    try:
        folders = list(versions.iterdir())
    except OSError:
        return DetectedValue(), DetectedValue(), DetectedValue()
    for folder in folders:
        if folder.is_symlink() or not folder.is_dir():
            continue
        result = parse_version_metadata(folder / f"{folder.name}.json")
        if result[0].value:
            candidates.append(result)
    # Several versions can use the same mods directory; guessing which one is
    # currently selected would mislabel the shared installation.
    return (
        candidates[0]
        if len(candidates) == 1
        else (DetectedValue(), DetectedValue(), DetectedValue())
    )


def valid_game(root: Path) -> bool:
    return (
        root.is_dir()
        and not root.is_symlink()
        and (
            (root / "versions").is_dir()
            or (root / "launcher_profiles.json").is_file()
            or (root / "mods").is_dir()
            or (root / "config").is_dir()
        )
    )


def legacy_subfolders(root: Path) -> list[MinecraftInstance]:
    """Legacy Launcher keeps isolated game content under game-directory/home/."""
    parent = root / "home"
    if parent.is_symlink() or not parent.is_dir():
        return []
    instances = []
    try:
        children = sorted(parent.iterdir())
    except OSError:
        return []
    for child in children:
        if not valid_game(child):
            continue
        minecraft, loader, loader_version = version_from_game(child)
        match = MC_RE.search(child.name)
        if not minecraft.value and match:
            minecraft = DetectedValue(match.group(), "inferred", "Legacy subfolder name")
        loader_name = next(
            (
                name
                for name in ("neoforge", "forge", "fabric", "quilt")
                if name in child.name.lower()
            ),
            "vanilla" if match else None,
        )
        if not loader.value and loader_name:
            loader = DetectedValue(loader_name, "inferred", "Legacy subfolder name")
        instances.append(
            MinecraftInstance("legacy", child.name, child, child, minecraft, loader, loader_version)
        )
    return instances


@dataclass
class SharedLauncherAdapter:
    client: str
    home: Path
    extra_roots: tuple[Path, ...] = ()

    def config_files(self) -> list[Path]:
        data = data_dir(self.home).parent
        cfg = config_dir(self.home).parent
        windows_tlauncher = roaming_dir(self.home) / ".tlauncher"
        if self.client == "legacy":
            files = [
                self.home / ".tlauncher/legacy.properties",
                self.home / ".tlauncher/legacy/Minecraft/tl.properties",
                self.home / ".launcher/legacy.properties",
                cfg / "legacylauncher/legacy.properties",
                data / "LegacyLauncher/launcher/config/legacy.properties",
            ]
            if sys.platform == "win32":
                files.extend(
                    (
                        windows_tlauncher / "legacy.properties",
                        windows_tlauncher / "legacy/Minecraft/tl.properties",
                    )
                )
            return files
        if self.client == "tlauncher":
            files = [
                self.home / ".tlauncher/tlauncher-2.0.properties",
                self.home / ".tlauncher/tlauncher.properties",
            ]
            if sys.platform == "win32":
                files.extend(
                    (
                        windows_tlauncher / "tlauncher-2.0.properties",
                        windows_tlauncher / "tlauncher.properties",
                    )
                )
            return files
        if self.client == "sklauncher":
            files = [
                self.home / ".sklauncher/instances.json",
                minecraft_dir(self.home) / "sklauncher/installations.json",
                minecraft_dir(self.home) / "sklauncher/profiles.json",
                cfg / "sklauncher/installations.json",
            ]
            if sys.platform == "win32":
                files.append(roaming_dir(self.home) / ".sklauncher/instances.json")
            return files
        return []

    def installed(self) -> bool:
        return (
            any(p.is_file() and not p.is_symlink() for p in self.config_files())
            or any(valid_game(root) for root in self.extra_roots)
            or (
                self.client == "legacy"
                and any(legacy_subfolders(root) for root in self.extra_roots)
            )
            or (
                self.client == "sklauncher"
                and (
                    (minecraft_dir(self.home) / "sklauncher").is_dir()
                    or (self.home / ".sklauncher").is_dir()
                )
            )
        )

    def scan(self) -> list[MinecraftInstance]:
        if not self.installed():
            return []
        fallback = minecraft_dir(self.home)
        results = []
        explicit_directory = False
        for config in self.config_files():
            if not config.is_file() or config.is_symlink():
                continue
            if config.suffix == ".properties":
                values = properties(config)
                root = configured_directory(values, fallback)
                explicit_directory = explicit_directory or root != fallback
                subfolders = []
                if self.client == "legacy":
                    subfolders = legacy_subfolders(root)
                    results.extend(subfolders)
                if not valid_game(root):
                    continue
                if subfolders and not any((root / name).is_dir() for name in CONTENT_DIRS):
                    continue
                selected = next((values[k] for k in VERSION_KEYS if values.get(k)), None)
                mc, loader, lv = version_from_game(root, selected)
                results.append(
                    MinecraftInstance(self.client, "Minecraft", root, root, mc, loader, lv)
                )
            else:
                data = read_json(config)
                entries = data.get("installations", data.get("profiles", []))
                if config.name == "instances.json":
                    entries = data.get("instances", [])
                if isinstance(entries, dict):
                    entries = list(entries.values())
                if not isinstance(entries, list):
                    entries = []
                root = Path(
                    str(
                        data.get("workDir")
                        or data.get("gameDir")
                        or (config.parent if config.name == "instances.json" else fallback)
                    )
                ).expanduser()
                explicit_directory = explicit_directory or root != fallback
                if not root.is_absolute():
                    root = fallback
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    game = Path(
                        str(
                            entry.get("directory")
                            or entry.get("gameDir")
                            or entry.get("gameDirectory")
                            or root
                        )
                    ).expanduser()
                    if not game.is_absolute() or not valid_game(game):
                        continue
                    version = entry.get("versionId") or entry.get("lastVersionId")
                    mc, loader, lv = version_from_game(root, str(version) if version else None)
                    if not mc.value and entry.get("minecraftVersion") not in (
                        None,
                        "latest-release",
                        "latest-snapshot",
                    ):
                        mc = detected(entry["minecraftVersion"])
                    if not loader.value and str(entry.get("gameType", "")).lower() in {
                        "vanilla",
                        "fabric",
                        "forge",
                        "neoforge",
                        "quilt",
                    }:
                        loader = detected(str(entry["gameType"]).lower())
                    results.append(
                        MinecraftInstance(
                            self.client,
                            str(entry.get("name") or "Minecraft"),
                            game,
                            game,
                            mc,
                            loader,
                            lv,
                        )
                    )
                if not entries and valid_game(root):
                    mc, loader, lv = version_from_game(root)
                    results.append(
                        MinecraftInstance(self.client, "Minecraft", root, root, mc, loader, lv)
                    )
        for root in self.extra_roots:
            subfolders = []
            if self.client == "legacy":
                subfolders = legacy_subfolders(root)
                results.extend(subfolders)
            if valid_game(root) and (
                not subfolders or any((root / name).is_dir() for name in CONTENT_DIRS)
            ):
                mc, loader, lv = version_from_game(root)
                results.append(
                    MinecraftInstance(self.client, root.name, root, root, mc, loader, lv)
                )
        if not results and not explicit_directory and valid_game(fallback):
            mc, loader, lv = version_from_game(fallback)
            results.append(
                MinecraftInstance(self.client, "Minecraft", fallback, fallback, mc, loader, lv)
            )
        unique: dict[Path, MinecraftInstance] = {}
        for instance in results:
            unique.setdefault(instance.game_dir.resolve(), instance)
        return list(unique.values())
