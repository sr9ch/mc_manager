from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def config_path() -> Path:
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mc_manager/config.toml"
    )


def data_path() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "mc_manager"


@dataclass
class Config:
    repository: Path | None = None
    remote: str | None = None
    ask_before_sync: bool = True
    git_commit: bool = True
    git_push: bool = False
    git_pull: bool = False
    excluded_clients: list[str] = field(default_factory=list)
    excluded_instances: list[str] = field(default_factory=list)
    extra_roots: dict[str, list[Path]] = field(default_factory=dict)
    destinations: dict[str, str] = field(default_factory=dict)
    ignores: list[str] = field(default_factory=list)


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config()
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    repo = data.get("repository", {})
    sync = data.get("sync", {})
    selection = data.get("selection", {})
    return Config(
        repository=Path(repo["path"]).expanduser() if repo.get("path") else None,
        remote=repo.get("remote"),
        ask_before_sync=bool(sync.get("ask_before_sync", True)),
        git_commit=bool(sync.get("git_commit", True)),
        git_push=bool(sync.get("git_push", False)),
        git_pull=bool(sync.get("git_pull", False)),
        excluded_clients=list(selection.get("excluded_clients", [])),
        excluded_instances=list(selection.get("excluded_instances", [])),
        extra_roots={
            k: [Path(p).expanduser() for p in v] for k, v in data.get("roots", {}).items()
        },
        destinations=dict(data.get("destinations", {})),
        ignores=list(sync.get("ignores", [])),
    )


def save(config: Config, path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    q = json.dumps
    lines = [
        "[repository]",
        f"path = {q(str(config.repository) if config.repository else '')}",
        f"remote = {q(config.remote or '')}",
        "",
        "[sync]",
        f"ask_before_sync = {str(config.ask_before_sync).lower()}",
        f"git_commit = {str(config.git_commit).lower()}",
        f"git_push = {str(config.git_push).lower()}",
        f"git_pull = {str(config.git_pull).lower()}",
        f"ignores = {q(config.ignores)}",
        "",
        "[selection]",
        f"excluded_clients = {q(config.excluded_clients)}",
        f"excluded_instances = {q(config.excluded_instances)}",
        "",
        "[roots]",
    ]
    lines.extend(
        f"{q(k)} = {q([str(p) for p in v])}" for k, v in sorted(config.extra_roots.items())
    )
    lines.extend(["", "[destinations]"])
    lines.extend(f"{q(k)} = {q(v)}" for k, v in sorted(config.destinations.items()))
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
