"""Bounded filesystem discovery for launchers without dedicated adapters."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .discovery import parse_mmc_pack, parse_profile, read_json
from .models import DetectedValue, MinecraftInstance
from .shared_launchers import version_from_game

MAX_DEPTH = 6
MAX_VISITED = 4000
PRUNE = {
    ".git",
    ".cache",
    "node_modules",
    "trash",
    "target",
    "build",
    "dist",
    "venv",
    ".venv",
    "__pycache__",
    "downloads",
    "pictures",
    "videos",
    "documents",
    "libraries",
    "assets",
    "versions",
    "logs",
    "crash-reports",
    "saves",
    "mods",
    "config",
    "resourcepacks",
    "shaderpacks",
    "runtime",
    "natives",
    "steam",
    "steamapps",
    "userdata",
}
WEIGHTS = {
    "versions": 4,
    "libraries": 3,
    "assets": 3,
    "launcher_profiles.json": 3,
    "options.txt": 2,
    "mods": 1,
    "config": 1,
    "resourcepacks": 1,
    "shaderpacks": 1,
    "saves": 1,
    "defaultconfigs": 1,
}


@dataclass
class Fingerprint:
    score: int
    indicators: tuple[str, ...]
    accepted: bool


@dataclass
class GenericResult:
    instances: list[MinecraftInstance] = field(default_factory=list)
    debug: list[str] = field(default_factory=list)
    visited: int = 0


def fingerprint(path: Path) -> Fingerprint:
    indicators = []
    for marker in WEIGHTS:
        target = path / marker
        if target.is_symlink():
            continue
        if target.is_dir() if "." not in marker else target.is_file():
            indicators.append(marker)
    score = sum(WEIGHTS[k] for k in indicators)
    parent_metadata = any(
        (path.parent / name).is_file() and not (path.parent / name).is_symlink()
        for name in ("mmc-pack.json", "profile.json", "instance.json")
    )
    if parent_metadata:
        indicators.append("parent metadata")
        score += 3
    strong = any(
        i in indicators
        for i in ("versions", "libraries", "assets", "launcher_profiles.json", "parent metadata")
    )
    content = sum(
        i in indicators
        for i in ("mods", "config", "resourcepacks", "shaderpacks", "saves", "defaultconfigs")
    )
    accepted = (score >= 5 and strong) or content >= 4
    return Fingerprint(score, tuple(indicators), accepted)


def search_roots(home: Path, extra: list[Path] | None = None) -> list[Path]:
    roots = [
        home / ".minecraft",
        home / ".sklauncher/instances",
        Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")),
        Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")),
        Path(os.environ.get("XDG_STATE_HOME", home / ".local/state")),
        home / ".var/app",
    ]
    roots.extend(extra or [])
    return list(dict.fromkeys(roots))


def metadata(path: Path) -> tuple[DetectedValue, DetectedValue, DetectedValue]:
    parent = path.parent
    if (parent / "mmc-pack.json").is_file():
        return parse_mmc_pack(parent / "mmc-pack.json")
    for name in ("profile.json", "instance.json"):
        data = read_json(parent / name)
        if data:
            return parse_profile(data)
    return version_from_game(path)


class GenericFilesystemDiscovery:
    client = "generic"

    def __init__(
        self,
        home: Path,
        extra_roots: list[Path] | None = None,
        max_depth: int = MAX_DEPTH,
        max_visited: int = MAX_VISITED,
        prune: set[str] | None = None,
    ) -> None:
        self.roots = search_roots(home, extra_roots)
        self.max_depth = max_depth
        self.max_visited = max_visited
        self.prune = prune if prune is not None else PRUNE

    def scan(self, debug: bool = False) -> GenericResult:
        result = GenericResult()
        visited: set[tuple[int, int]] = set()
        queue: list[tuple[Path, int]] = [(p, 0) for p in self.roots]
        while queue and result.visited < self.max_visited:
            path, depth = queue.pop(0)
            try:
                if path.is_symlink() or not path.is_dir():
                    continue
                stat = path.stat()
                identity = (stat.st_dev, stat.st_ino)
                if identity in visited:
                    continue
                visited.add(identity)
                result.visited += 1
                fp = fingerprint(path)
                if fp.indicators and debug:
                    indicators = ",".join(fp.indicators)
                    result.debug.append(
                        f"Candidate {path}: score={fp.score}; indicators={indicators}; "
                        f"result={'accepted' if fp.accepted else 'rejected'}"
                    )
                if fp.accepted:
                    mc, loader, lv = metadata(path)
                    name = (
                        path.parent.name
                        if path.name in {"minecraft", ".minecraft", "game", "instance"}
                        else path.name
                    )
                    if path.name == ".minecraft":
                        name = "Minecraft"
                    result.instances.append(
                        MinecraftInstance(
                            "generic", name, path, path, mc, loader, lv, (), ("generic",)
                        )
                    )
                    if debug:
                        mc_detail = f"{mc.value or 'unknown'} ({mc.source or 'unknown'})"
                        loader_detail = (
                            f"{loader.value or 'unknown'} ({loader.source or 'unknown'})"
                        )
                        result.debug.append(
                            f"Metadata {path}: Minecraft={mc_detail}, loader={loader_detail}"
                        )
                    continue
                if depth >= self.max_depth:
                    continue
                with os.scandir(path) as entries:
                    children = [
                        Path(e.path)
                        for e in entries
                        if e.is_dir(follow_symlinks=False) and e.name.lower() not in self.prune
                    ]
                queue.extend((child, depth + 1) for child in children)
            except (OSError, PermissionError) as exc:
                if debug:
                    result.debug.append(f"Skipped {path}: {exc}")
        if queue and debug:
            result.debug.append(f"Stopped after {self.max_visited} directories")
        return result
