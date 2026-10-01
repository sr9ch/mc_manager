from __future__ import annotations

import fnmatch
import json
import logging
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .config import Config
from .models import Change, MinecraftInstance, Plan
from .safety import (
    CONTENT_DIRS,
    SafetyError,
    allowed_relative,
    assert_destination,
    assert_plain_directory,
    safe_content,
    safe_relative,
    sha256_file,
    slug,
    world_slug,
)

LOG = logging.getLogger(__name__)
MANIFEST = "mc_manager.json"


def destination_for(instance: MinecraftInstance, config: Config, used: set[str]) -> Path:
    import hashlib

    if instance.key in config.destinations:
        relative = safe_relative(config.destinations[instance.key])
    else:
        base = Path("minecraft") / slug(instance.client) / slug(instance.name)
        relative = base
        if relative.as_posix() in used:
            suffix = hashlib.sha256(instance.key.encode()).hexdigest()[:8]
            relative = base.with_name(f"{base.name}-{suffix}")
        config.destinations[instance.key] = relative.as_posix()
    if not relative.parts[:1] == ("minecraft",) or len(relative.parts) != 3:
        raise SafetyError(f"Invalid managed destination: {relative}")
    if relative.as_posix() in used:
        raise SafetyError(f"Destination collision: {relative}")
    used.add(relative.as_posix())
    return relative


def _ignored(relative: Path, patterns: list[str]) -> bool:
    value = relative.as_posix()
    return any(fnmatch.fnmatch(value, p) or fnmatch.fnmatch(relative.name, p) for p in patterns)


def _walk_content(
    source_dir: Path, prefix: Path, game: Path, patterns: list[str]
) -> dict[str, Path]:
    found: dict[str, Path] = {}
    if not source_dir.is_dir() or source_dir.is_symlink():
        return found
    for current, dirs, files in os.walk(source_dir, followlinks=False):
        current_path = Path(current)
        dirs[:] = [
            d
            for d in dirs
            if not (current_path / d).is_symlink()
            and allowed_relative(Path(d))
            and not _ignored(prefix / current_path.relative_to(source_dir) / d, patterns)
        ]
        for filename in files:
            source = current_path / filename
            relative = prefix / current_path.relative_to(source_dir) / filename
            if (
                source.is_symlink()
                or not source.is_file()
                or not allowed_relative(relative)
                or _ignored(relative, patterns)
            ):
                continue
            if not source.resolve().is_relative_to(game.resolve()):
                raise SafetyError(f"Source escapes game directory: {source}")
            try:
                if safe_content(source, relative.parts[0]):
                    found[relative.as_posix()] = source
                else:
                    LOG.warning("Skipped possible secret or oversized file: %s", source)
            except OSError as exc:
                LOG.warning("Cannot inspect %s: %s", source, exc)
    return found


def source_files(instance: MinecraftInstance, patterns: list[str] | None = None) -> dict[str, Path]:
    game = instance.game_dir
    assert_plain_directory(game)
    patterns = patterns or []
    local_ignore = game / ".mcmanagerignore"
    if local_ignore.is_file() and not local_ignore.is_symlink():
        patterns = [
            *patterns,
            *(
                line.strip()
                for line in local_ignore.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ),
        ]
    found: dict[str, Path] = {}
    for name in CONTENT_DIRS:
        found.update(_walk_content(game / name, Path(name), game, patterns))
    saves = game / "saves"
    if saves.is_dir() and not saves.is_symlink():
        for world in saves.iterdir():
            if world.is_symlink() or not world.is_dir():
                continue
            prefix = Path("datapacks") / world_slug(world.name)
            found.update(_walk_content(world / "datapacks", prefix, game, patterns))
    return found


def _manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise SafetyError(f"Unsafe manifest: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SafetyError(f"Cannot read manifest {path}: {exc}") from exc
    if data.get("schema") != 1 or not isinstance(data.get("files"), dict):
        raise SafetyError(f"Unknown manifest schema: {path}")
    for name, digest in data["files"].items():
        safe_relative(name)
        if not isinstance(digest, str) or len(digest) != 64:
            raise SafetyError(f"Invalid hash in {path}")
    return data


def _metadata(instance: MinecraftInstance) -> dict:
    return {
        "name": instance.name,
        "client": instance.client,
        "launchers": list(instance.launchers or (instance.client,)),
        "discovery_sources": list(instance.discovery_sources or (instance.client,)),
        "minecraft": {
            "version": instance.minecraft.value,
            "confidence": instance.minecraft.confidence,
        },
        "loader": {
            "type": instance.loader.value,
            "version": instance.loader_version.value,
            "confidence": instance.loader.confidence,
        },
    }


def plan_instance(
    instance: MinecraftInstance, repository: Path, relative: Path, ignores: list[str] | None = None
) -> Plan:
    assert_plain_directory(repository)
    if repository.resolve().is_relative_to(
        instance.game_dir.resolve()
    ) or instance.game_dir.resolve().is_relative_to(repository.resolve()):
        raise SafetyError("Repository and game directory must be separate")
    destination = repository / relative
    assert_destination(repository, relative / MANIFEST)
    old = _manifest(destination / MANIFEST)
    owned: dict[str, str] = old.get("files", {})
    current = source_files(instance, ignores)
    hashes: dict[str, str] = {}
    changes: list[Change] = []
    for name, source in sorted(current.items()):
        target = assert_destination(repository, relative / safe_relative(name))
        new_hash = sha256_file(source)
        hashes[name] = new_hash
        old_hash = owned.get(name)
        if target.exists():
            target_hash = sha256_file(target)
            if old_hash is None:
                raise SafetyError(f"Unmanaged file collision: {target}")
            if old_hash is not None and target_hash != old_hash and target_hash != new_hash:
                raise SafetyError(f"Managed file edited in repository: {target}")
            if target_hash != new_hash:
                changes.append(Change("modify", name, source, new_hash))
        else:
            changes.append(Change("add", name, source, new_hash))
    for name, old_hash in sorted(owned.items()):
        if name in hashes:
            continue
        target = assert_destination(repository, relative / safe_relative(name))
        if target.exists():
            if sha256_file(target) != old_hash:
                raise SafetyError(f"Managed file edited in repository: {target}")
            changes.append(Change("delete", name))
    metadata_changed = old.get("instance") != _metadata(instance) or old.get("files") != hashes
    return Plan(instance, destination, changes, hashes, metadata_changed)


def apply_plan(plan: Plan, repository: Path) -> list[Path]:
    assert_plain_directory(repository)
    relative = plan.destination.relative_to(repository)
    written: list[Path] = []
    old = _manifest(plan.destination / MANIFEST)
    for change in plan.changes:
        target = assert_destination(repository, relative / safe_relative(change.path))
        if change.kind == "delete":
            if not target.exists() or sha256_file(target) != old.get("files", {}).get(change.path):
                raise SafetyError(f"Destination changed since scan: {target}")
            target.unlink()
        else:
            assert change.source is not None and change.sha256 is not None
            if (
                change.source.is_symlink()
                or sha256_file(change.source) != change.sha256
                or not safe_content(change.source, Path(change.path).parts[0])
            ):
                raise SafetyError(f"Source changed since scan: {change.source}")
            if target.exists():
                existing_hash = sha256_file(target)
                if existing_hash not in {old.get("files", {}).get(change.path), change.sha256}:
                    raise SafetyError(f"Destination changed since scan: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=".mc_manager-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as out:
                    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    with os.fdopen(os.open(change.source, flags), "rb") as source:
                        shutil.copyfileobj(source, out, 1024 * 1024)
                if sha256_file(Path(temp_name)) != change.sha256:
                    raise SafetyError(f"Source changed during copy: {change.source}")
                os.replace(temp_name, target)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        written.append(target)
    if plan.changed:
        assert_destination(repository, relative / MANIFEST)
        plan.destination.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema": 1,
            "instance": _metadata(plan.instance),
            "files": plan.files,
            "last_sync": datetime.now(UTC).isoformat(),
        }
        fd, temp_name = tempfile.mkstemp(prefix=".mc_manager-", dir=plan.destination)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, indent=2, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
            os.replace(temp_name, plan.destination / MANIFEST)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        written.append(plan.destination / MANIFEST)
    return written
