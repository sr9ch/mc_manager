from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DetectedValue:
    value: str | None = None
    confidence: str = "unknown"  # detected, inferred, unknown
    source: str | None = None


@dataclass(frozen=True)
class MinecraftInstance:
    client: str
    name: str
    root: Path
    game_dir: Path
    minecraft: DetectedValue = field(default_factory=DetectedValue)
    loader: DetectedValue = field(default_factory=DetectedValue)
    loader_version: DetectedValue = field(default_factory=DetectedValue)
    launchers: tuple[str, ...] = ()
    discovery_sources: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return str(self.game_dir.resolve())


@dataclass(frozen=True)
class Change:
    kind: str  # add, modify, delete
    path: str
    source: Path | None = None
    sha256: str | None = None


@dataclass
class Plan:
    instance: MinecraftInstance
    destination: Path
    changes: list[Change]
    files: dict[str, str]
    metadata_changed: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.changes or self.metadata_changed)
