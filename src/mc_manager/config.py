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
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid configuration {path}: {exc}") from exc

    def table(name: str) -> dict:
        value = data.get(name, {})
        if not isinstance(value, dict):
            raise ValueError(f"Invalid configuration {path}: [{name}] must be a table")
        return value

    def strings(value: object, name: str) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"Invalid configuration {path}: {name} must be a list of strings")
        return value

    repo = table("repository")
    sync = table("sync")
    selection = table("selection")
    roots = table("roots")
    destinations = table("destinations")
    for name in ("path", "remote"):
        if not isinstance(repo.get(name, ""), str):
            raise ValueError(f"Invalid configuration {path}: repository.{name} must be text")
    for name in ("ask_before_sync", "git_commit", "git_push", "git_pull"):
        if not isinstance(sync.get(name, True), bool):
            raise ValueError(f"Invalid configuration {path}: sync.{name} must be true or false")
    if not all(isinstance(value, str) for value in destinations.values()):
        raise ValueError(f"Invalid configuration {path}: destinations must contain text paths")
    return Config(
        repository=Path(repo["path"]).expanduser() if repo.get("path") else None,
        remote=repo.get("remote"),
        ask_before_sync=sync.get("ask_before_sync", True),
        git_commit=sync.get("git_commit", True),
        git_push=sync.get("git_push", False),
        git_pull=sync.get("git_pull", False),
        excluded_clients=strings(
            selection.get("excluded_clients", []), "selection.excluded_clients"
        ),
        excluded_instances=strings(
            selection.get("excluded_instances", []), "selection.excluded_instances"
        ),
        extra_roots={
            k: [Path(p).expanduser() for p in strings(v, f"roots.{k}")] for k, v in roots.items()
        },
        destinations=dict(destinations),
        ignores=strings(sync.get("ignores", []), "sync.ignores"),
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
