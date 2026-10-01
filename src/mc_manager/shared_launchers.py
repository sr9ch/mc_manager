"""Launchers that may share one Minecraft game directory."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .discovery import detected, parse_version_metadata, read_json
from .models import DetectedValue, MinecraftInstance


def properties(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue
        values[key.strip().lower()] = value.strip()
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
VERSION_KEYS = ("selectedversion", "minecraft.version", "version", "login.version", "lastversion")


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
    for folder in versions.iterdir():
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


@dataclass
class SharedLauncherAdapter:
    client: str
    home: Path

    def config_files(self) -> list[Path]:
        data = Path(os.environ.get("XDG_DATA_HOME", self.home / ".local/share"))
        cfg = Path(os.environ.get("XDG_CONFIG_HOME", self.home / ".config"))
        if self.client == "legacy":
            return [
                self.home / ".launcher/legacy.properties",
                cfg / "legacylauncher/legacy.properties",
                data / "LegacyLauncher/launcher/config/legacy.properties",
            ]
        if self.client == "tlauncher":
            return [
                self.home / ".tlauncher/tlauncher-2.0.properties",
                self.home / ".tlauncher/tlauncher.properties",
            ]
        if self.client == "sklauncher":
            return [
                self.home / ".sklauncher/instances.json",
                self.home / ".minecraft/sklauncher/installations.json",
                self.home / ".minecraft/sklauncher/profiles.json",
                cfg / "sklauncher/installations.json",
            ]
        return []

    def installed(self) -> bool:
        return any(p.is_file() and not p.is_symlink() for p in self.config_files()) or (
            self.client == "sklauncher"
            and (
                (self.home / ".minecraft/sklauncher").is_dir()
                or (self.home / ".sklauncher").is_dir()
            )
        )

    def scan(self) -> list[MinecraftInstance]:
        if not self.installed():
            return []
        fallback = self.home / ".minecraft"
        results = []
        explicit_directory = False
        for config in self.config_files():
            if not config.is_file() or config.is_symlink():
                continue
            if config.suffix == ".properties":
                values = properties(config)
                root = configured_directory(values, fallback)
                explicit_directory = explicit_directory or root != fallback
                if not valid_game(root):
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
        if not results and not explicit_directory and valid_game(fallback):
            mc, loader, lv = version_from_game(fallback)
            results.append(
                MinecraftInstance(self.client, "Minecraft", fallback, fallback, mc, loader, lv)
            )
        return results
