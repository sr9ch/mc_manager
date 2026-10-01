from __future__ import annotations

import configparser
import json
import logging
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .models import DetectedValue, MinecraftInstance

LOG = logging.getLogger(__name__)


def detected(value: object, source: str = "launcher metadata") -> DetectedValue:
    return (
        DetectedValue(str(value), "detected", source)
        if value not in (None, "")
        else DetectedValue()
    )


def inferred(value: object, source: str = "version identifier") -> DetectedValue:
    return (
        DetectedValue(str(value), "inferred", source)
        if value not in (None, "")
        else DetectedValue()
    )


def read_json(path: Path) -> dict:
    try:
        if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
            return {}
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def first(data: dict, *keys: str) -> object:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


LOADER_RE = re.compile(r"(?i)(neoforge|forge|fabric|quilt)[-_ ](?:loader[-_ ]?)?([0-9][\w.+-]*)")
MC_RE = re.compile(r"(?<!\d)(1\.\d+(?:\.\d+)?)(?!\d)")


def parse_version_id(version_id: str) -> tuple[DetectedValue, DetectedValue, DetectedValue]:
    mc = MC_RE.search(version_id)
    loader = LOADER_RE.search(version_id)
    loader_version = loader.group(2) if loader else None
    if loader_version and mc and loader_version.endswith("-" + mc.group(1)):
        loader_version = loader_version[: -len(mc.group(1)) - 1]
    return (
        inferred(mc.group(1) if mc else None),
        inferred(loader.group(1).lower() if loader else "vanilla" if mc else None),
        inferred(loader_version),
    )


def parse_version_metadata(path: Path) -> tuple[DetectedValue, DetectedValue, DetectedValue]:
    data = read_json(path)
    if not data:
        return DetectedValue(), DetectedValue(), DetectedValue()
    mc, loader, lv = parse_version_id(str(data.get("id", "")))
    if data.get("inheritsFrom"):
        mc = detected(data["inheritsFrom"], "version JSON")
    for library in data.get("libraries", []):
        if not isinstance(library, dict):
            continue
        coordinate = str(library.get("name", ""))
        parts = coordinate.split(":")
        if len(parts) < 3:
            continue
        group, artifact, version = parts[:3]
        if group == "net.fabricmc" and artifact == "fabric-loader":
            loader, lv = detected("fabric", "version library"), detected(version, "version library")
        elif group == "org.quiltmc" and artifact == "quilt-loader":
            loader, lv = detected("quilt", "version library"), detected(version, "version library")
        elif group == "net.minecraftforge" and artifact == "forge":
            loader, lv = (
                detected("forge", "version library"),
                detected(version.split("-")[-1], "version library"),
            )
        elif group == "net.neoforged" and artifact == "neoforge":
            loader, lv = (
                detected("neoforge", "version library"),
                detected(version, "version library"),
            )
    if mc.value and not loader.value:
        loader = detected("vanilla", "version JSON")
    return mc, loader, lv


def parse_mmc_pack(path: Path) -> tuple[DetectedValue, DetectedValue, DetectedValue]:
    data = read_json(path)
    mc, loader, lv = DetectedValue(), DetectedValue(), DetectedValue()
    for component in data.get("components", []):
        if not isinstance(component, dict):
            continue
        uid = str(component.get("uid", "")).lower()
        version = component.get("version")
        if uid == "net.minecraft":
            mc = detected(version, "mmc-pack.json")
        for marker, name in (
            ("net.fabricmc.fabric-loader", "fabric"),
            ("org.quiltmc.quilt-loader", "quilt"),
            ("net.minecraftforge", "forge"),
            ("net.neoforged", "neoforge"),
        ):
            if uid.startswith(marker):
                loader, lv = detected(name, "mmc-pack.json"), detected(version, "mmc-pack.json")
    if mc.value and not loader.value:
        loader = detected("vanilla", "mmc-pack.json")
    return mc, loader, lv


def parse_profile(data: dict) -> tuple[DetectedValue, DetectedValue, DetectedValue]:
    game = first(data, "game_version", "gameVersion", "minecraftVersion", "minecraft_version")
    loader_raw = first(data, "loader", "modloader", "modLoader", "loader_type")
    loader_version = first(data, "loader_version", "loaderVersion", "modLoaderVersion")
    if isinstance(loader_raw, dict):
        loader_version = loader_version or first(loader_raw, "version", "loaderVersion")
        loader_raw = first(loader_raw, "type", "name")
    if isinstance(data.get("baseModLoader"), dict):
        base = data["baseModLoader"]
        loader_raw = loader_raw or first(base, "type", "name")
        loader_version = loader_version or first(base, "version")
    loader = str(loader_raw).lower() if loader_raw else None
    if loader:
        for candidate in ("neoforge", "fabric", "quilt", "forge"):
            if candidate in loader:
                loader = candidate
                break
    if game and not loader:
        loader = "vanilla"
    return detected(game), detected(loader), detected(loader_version)


def candidate_roots(home: Path, client: str) -> list[Path]:
    data = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
    if client == "prism":
        return [
            data / "PrismLauncher/instances",
            home / ".var/app/org.prismlauncher.PrismLauncher/data/PrismLauncher/instances",
            home / "PrismLauncher/instances",
        ]
    if client == "multimc":
        return [
            home / "MultiMC/instances",
            data / "MultiMC/instances",
            home / ".local/share/multimc/instances",
        ]
    if client == "modrinth":
        return [
            data / "ModrinthApp/profiles",
            home / ".var/app/com.modrinth.ModrinthApp/data/ModrinthApp/profiles",
            home / ".local/share/ModrinthApp/profiles",
        ]
    if client == "atlauncher":
        return [home / "ATLauncher/instances", data / "ATLauncher/instances"]
    if client == "curseforge":
        return [
            home / "curseforge/minecraft/Instances",
            home / "Documents/CurseForge/Minecraft/Instances",
            data / "CurseForge/Minecraft/Instances",
        ]
    if client == "gdlauncher":
        return [home / "gdlauncher_next/instances", data / "gdlauncher_next/instances"]
    return []


class DirectoryAdapter:
    def __init__(self, client: str, home: Path, extra_roots: Iterable[Path] = ()) -> None:
        self.client = client
        self.roots = [*candidate_roots(home, client), *extra_roots]

    def scan(self) -> list[MinecraftInstance]:
        found: list[MinecraftInstance] = []
        for root in dict.fromkeys(self.roots):
            if not root.is_dir() or root.is_symlink():
                continue
            try:
                children = sorted(root.iterdir())
            except OSError as exc:
                LOG.warning("Cannot list %s: %s", root, exc)
                continue
            for child in children:
                if not child.is_dir() or child.is_symlink():
                    continue
                instance = self.parse(child)
                if instance:
                    found.append(instance)
        return found

    def installed(self) -> bool:
        if any(root.is_dir() and not root.is_symlink() for root in self.roots):
            return True
        return self.client == "prism" and (self.roots[1].parents[2]).is_dir()

    def parse(self, root: Path) -> MinecraftInstance | None:
        client = self.client
        if client in {"prism", "multimc"}:
            manifest = root / "mmc-pack.json"
            if not manifest.is_file():
                return None
            cfg = configparser.ConfigParser(interpolation=None)
            try:
                cfg.read_string(
                    "[instance]\n" + (root / "instance.cfg").read_text(encoding="utf-8-sig")
                )
                name = cfg["instance"].get("name", root.name)
            except (OSError, UnicodeError, configparser.Error):
                name = root.name
            mc, loader, lv = parse_mmc_pack(manifest)
            game = root / "minecraft" if (root / "minecraft").is_dir() else root / ".minecraft"
        else:
            filenames = {
                "modrinth": ("profile.json",),
                "atlauncher": ("instance.json",),
                "curseforge": ("minecraftinstance.json",),
                "gdlauncher": ("instance.json",),
            }[client]
            manifest = next((root / n for n in filenames if (root / n).is_file()), None)
            if not manifest:
                return None
            data = read_json(manifest)
            if not data:
                return None
            # The supported keys are deliberately narrow; unknown schemas stay unknown.
            name = str(first(data, "name", "instanceName", "profileName") or root.name)
            if client == "modrinth":
                mc, loader, lv = parse_profile(data)
                game = root
            elif client == "atlauncher":
                mc, loader, lv = parse_profile(data)
                game = root
            elif client == "curseforge":
                mc, loader, lv = parse_profile(data)
                game = root
            else:
                mc, loader, lv = parse_profile(data)
                game = root / "instance" if (root / "instance").is_dir() else root
        if (
            game.is_symlink()
            or not game.is_dir()
            or not game.resolve().is_relative_to(root.resolve())
        ):
            return None
        return MinecraftInstance(client, name, root, game, mc, loader, lv)


class VanillaAdapter:
    def __init__(self, home: Path) -> None:
        self.home = home

    def scan(self) -> list[MinecraftInstance]:
        roots = [self.home / ".minecraft"]
        if sys.platform == "darwin":
            roots.append(self.home / "Library/Application Support/minecraft")
        if os.name == "nt":
            roots.append(Path(os.environ.get("APPDATA", self.home)) / ".minecraft")
        found = []
        for root in roots:
            if root.is_symlink() or not root.is_dir():
                continue
            profiles = read_json(root / "launcher_profiles.json").get("profiles", {})
            if not isinstance(profiles, dict):
                continue
            for key, profile in profiles.items():
                if not isinstance(profile, dict):
                    continue
                version_id = str(profile.get("lastVersionId", ""))
                game = Path(profile.get("gameDir") or root).expanduser()
                if not game.is_absolute():
                    game = root / game
                if game.is_symlink() or not game.is_dir():
                    continue
                mc, loader, lv = parse_version_id(version_id)
                version_data = (
                    read_json(root / "versions" / version_id / f"{version_id}.json")
                    if version_id
                    else {}
                )
                if version_data.get("inheritsFrom"):
                    mc = detected(version_data["inheritsFrom"])
                elif version_data.get("id") and not mc.value:
                    mc = inferred(version_data["id"])
                found.append(
                    MinecraftInstance(
                        "vanilla", str(profile.get("name") or key), game, game, mc, loader, lv
                    )
                )
        return found


@dataclass
class DiscoveryReport:
    instances: list[MinecraftInstance]
    clients: dict[str, int]
    installed: set[str]
    debug: list[str]
    duplicates_merged: int = 0


def _merge_value(values: list[DetectedValue]) -> DetectedValue:
    known = [v for v in values if v.value]
    if not known:
        return DetectedValue()
    if len({v.value.lower() for v in known if v.value}) > 1:
        return DetectedValue()
    return next((v for v in known if v.confidence == "detected"), known[0])


def discover_report(
    home: Path | None = None, extra_roots: dict[str, list[Path]] | None = None, debug: bool = False
) -> DiscoveryReport:
    from .generic import GenericFilesystemDiscovery
    from .shared_launchers import SharedLauncherAdapter

    home = home or Path.home()
    extra_roots = extra_roots or {}
    adapters = [
        DirectoryAdapter(name, home, extra_roots.get(name, []))
        for name in ("prism", "multimc", "modrinth", "atlauncher", "curseforge", "gdlauncher")
    ]
    adapters.append(VanillaAdapter(home))
    adapters.extend(
        SharedLauncherAdapter(name, home) for name in ("legacy", "tlauncher", "sklauncher")
    )
    extra = [root for values in extra_roots.values() for root in values]
    extra.extend(
        root
        for adapter in adapters
        if isinstance(adapter, DirectoryAdapter)
        for root in adapter.roots
    )
    generic = GenericFilesystemDiscovery(home, extra).scan(debug=debug)
    groups: dict[tuple[int, int], list[MinecraftInstance]] = {}
    counts: dict[str, int] = {}
    installed: set[str] = set()
    for adapter in adapters:
        matches = adapter.scan()
        counts[adapter.client if hasattr(adapter, "client") else "vanilla"] = len(matches)
        if (
            matches
            or (hasattr(adapter, "installed") and adapter.installed())
            or (
                isinstance(adapter, DirectoryAdapter)
                and any(root.is_dir() for root in adapter.roots)
            )
        ):
            installed.add(adapter.client if hasattr(adapter, "client") else "vanilla")
        for instance in matches:
            stat = instance.game_dir.stat()
            groups.setdefault((stat.st_dev, stat.st_ino), []).append(instance)
    counts["generic"] = len(generic.instances)
    if generic.instances:
        installed.add("generic")
    for instance in generic.instances:
        stat = instance.game_dir.stat()
        groups.setdefault((stat.st_dev, stat.st_ino), []).append(instance)
    result = []
    priority = {"prism": 0, "legacy": 1, "tlauncher": 2, "sklauncher": 3, "vanilla": 4}
    for identity, matches in groups.items():
        matches.sort(key=lambda i: (priority.get(i.client, 10), i.client))
        primary = matches[0]
        launchers = tuple(dict.fromkeys(i.client for i in matches if i.client != "generic"))
        sources = tuple(dict.fromkeys(i.client for i in matches))
        if debug and len(matches) > 1:
            generic.debug.append(
                f"Merged {len(matches)} results for {primary.game_dir} by device/inode {identity}"
            )
        result.append(
            MinecraftInstance(
                primary.client,
                primary.name,
                primary.root,
                primary.game_dir,
                _merge_value([i.minecraft for i in matches]),
                _merge_value([i.loader for i in matches]),
                _merge_value([i.loader_version for i in matches]),
                launchers,
                sources,
            )
        )
    merged = sum(len(v) - 1 for v in groups.values())
    return DiscoveryReport(
        sorted(result, key=lambda x: (x.client, x.name.lower(), str(x.root))),
        counts,
        installed,
        generic.debug,
        merged,
    )


def discover(
    home: Path | None = None, extra_roots: dict[str, list[Path]] | None = None
) -> list[MinecraftInstance]:
    return discover_report(home, extra_roots).instances
