"""Recoverable repository writes for one or more Minecraft instances."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .models import Plan
from .safety import (
    SafetyError,
    assert_destination,
    assert_plain_directory,
    safe_content,
    safe_relative,
    sha256_file,
)
from .sync import MANIFEST, _manifest, _metadata

TRANSACTION = ".mc_manager-transaction"
JOURNAL = "journal.json"
SCHEMA = 1
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_journal(root: Path, phase: str, operations: list[dict]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".journal-", dir=root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"schema": SCHEMA, "phase": phase, "operations": operations}, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / JOURNAL)
        _sync_directory(root)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _cleanup(root: Path) -> None:
    for name in ("new", "old"):
        directory = root / name
        if directory.is_symlink():
            raise SafetyError(f"Unsafe transaction directory: {directory}")
        if directory.exists():
            shutil.rmtree(directory)
    for temporary in root.glob(".journal-*"):
        if temporary.is_symlink() or not temporary.is_file():
            raise SafetyError(f"Unsafe transaction file: {temporary}")
        temporary.unlink()
    journal = root / JOURNAL
    if journal.exists():
        journal.unlink()
    root.rmdir()


def _validate_operations(operations: object) -> list[dict]:
    if not isinstance(operations, list):
        raise SafetyError("Invalid transaction journal: operations must be a list")
    seen: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) != {"target", "old_hash", "new_hash"}:
            raise SafetyError("Invalid transaction journal: operation")
        target = operation["target"]
        if not isinstance(target, str):
            raise SafetyError("Invalid transaction journal: target")
        relative = safe_relative(target)
        if relative.parts[0] != "minecraft" or target in seen:
            raise SafetyError("Invalid transaction journal: target")
        seen.add(target)
        for key in ("old_hash", "new_hash"):
            digest = operation[key]
            if digest is not None and (
                not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
            ):
                raise SafetyError("Invalid transaction journal: hash")
        if operation["old_hash"] is None and operation["new_hash"] is None:
            raise SafetyError("Invalid transaction journal: empty operation")
    return operations


def _rollback(repository: Path, root: Path, operations: list[dict]) -> None:
    for index in reversed(range(len(operations))):
        operation = operations[index]
        target = assert_destination(repository, safe_relative(operation["target"]))
        backup = root / "old" / str(index)
        if backup.is_symlink():
            raise SafetyError(f"Unsafe transaction backup: {backup}")
        current_hash = sha256_file(target) if target.exists() else None
        old_hash = operation["old_hash"]
        new_hash = operation["new_hash"]
        if backup.exists():
            if sha256_file(backup) != old_hash or current_hash not in {None, old_hash, new_hash}:
                raise SafetyError(f"Cannot safely restore {target}; inspect {root}")
            os.replace(backup, target)
            _sync_directory(target.parent)
        elif old_hash is None:
            if current_hash not in {None, new_hash}:
                raise SafetyError(f"Cannot safely remove changed file {target}; inspect {root}")
            if current_hash is not None:
                target.unlink()
                _sync_directory(target.parent)
        elif current_hash != old_hash:
            raise SafetyError(f"Missing transaction backup for {target}; inspect {root}")


def recover_transaction(repository: Path) -> bool:
    """Restore a transaction interrupted before commit, leaving unknown data untouched."""
    root = repository / TRANSACTION
    if not root.exists() and not root.is_symlink():
        return False
    assert_plain_directory(root)
    journal = root / JOURNAL
    if not journal.exists():
        entries = list(root.iterdir())
        # A crash before the first journal rename (or after committed cleanup)
        # can leave only temporary journal files and empty staging directories.
        if all(
            (entry.name in {"new", "old"} and entry.is_dir() and not any(entry.iterdir()))
            or (entry.name.startswith(".journal-") and entry.is_file())
            for entry in entries
        ):
            _cleanup(root)
            return True
    if journal.is_symlink() or not journal.is_file():
        raise SafetyError(f"Transaction journal missing or unsafe: {journal}")
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SafetyError(f"Cannot read transaction journal {journal}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise SafetyError(f"Unknown transaction journal schema: {journal}")
    phase = data.get("phase")
    operations = _validate_operations(data.get("operations"))
    if phase == "ready":
        _rollback(repository, root, operations)
    elif phase not in {"staging", "committed"}:
        raise SafetyError(f"Unknown transaction phase: {phase}")
    _cleanup(root)
    return True


Progress = Callable[[str, int, int], None]


def _stage_file(
    source: Path,
    destination: Path,
    expected_hash: str,
    on_chunk: Callable[[int], None] | None = None,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(source, flags), "rb") as stream:
        with destination.open("xb") as output:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                output.write(block)
                if on_chunk:
                    on_chunk(len(block))
            output.flush()
            os.fsync(output.fileno())
    if sha256_file(destination) != expected_hash:
        raise SafetyError(f"Source changed during copy: {source}")


def _prepare(
    plans: list[Plan],
    repository: Path,
    root: Path,
    on_chunk: Callable[[int], None] | None = None,
) -> list[dict]:
    operations: list[dict] = []
    targets: set[str] = set()
    for plan in plans:
        if not plan.changed:
            continue
        relative = plan.destination.relative_to(repository)
        manifest_path = assert_destination(repository, relative / MANIFEST)
        old_manifest = _manifest(manifest_path)
        manifest_hash = sha256_file(manifest_path) if manifest_path.exists() else None
        if manifest_hash != plan.manifest_hash:
            raise SafetyError(f"Manifest changed since scan: {manifest_path}")
        old_files = old_manifest.get("files", {})
        for change in plan.changes:
            target = assert_destination(repository, relative / safe_relative(change.path))
            current_hash = sha256_file(target) if target.exists() else None
            old_hash = old_files.get(change.path)
            if change.kind == "add":
                if current_hash is not None:
                    raise SafetyError(f"Unmanaged file appeared since scan: {target}")
            elif change.kind == "delete":
                if old_hash is None or current_hash != old_hash:
                    raise SafetyError(f"Destination changed since scan: {target}")
            elif change.kind == "modify":
                if old_hash is None or current_hash not in {old_hash, change.sha256}:
                    raise SafetyError(f"Destination changed since scan: {target}")
            else:
                raise SafetyError(f"Unknown change type: {change.kind}")
            new_hash = None
            if change.kind != "delete":
                source = change.source
                if source is None or change.sha256 is None or source.is_symlink():
                    raise SafetyError(f"Source changed since scan: {source}")
                if sha256_file(source) != change.sha256 or not safe_content(
                    source, Path(change.path).parts[0]
                ):
                    raise SafetyError(f"Source changed since scan: {source}")
                new_hash = change.sha256
                _stage_file(source, root / "new" / str(len(operations)), new_hash, on_chunk)
            target_name = target.relative_to(repository).as_posix()
            if target_name in targets:
                raise SafetyError(f"Destination collision: {target}")
            targets.add(target_name)
            operations.append(
                {"target": target_name, "old_hash": current_hash, "new_hash": new_hash}
            )
        manifest = {
            "schema": 1,
            "instance": _metadata(plan.instance),
            "files": plan.files,
            "last_sync": datetime.now(UTC).isoformat(),
        }
        staged_manifest = root / "new" / str(len(operations))
        with staged_manifest.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        target_name = manifest_path.relative_to(repository).as_posix()
        if target_name in targets:
            raise SafetyError(f"Destination collision: {manifest_path}")
        targets.add(target_name)
        operations.append(
            {
                "target": target_name,
                "old_hash": manifest_hash,
                "new_hash": sha256_file(staged_manifest),
            }
        )
    return operations


def _apply(
    repository: Path, root: Path, operations: list[dict], progress: Progress | None = None
) -> list[Path]:
    written = []
    for index, operation in enumerate(operations):
        target = assert_destination(repository, safe_relative(operation["target"]))
        current_hash = sha256_file(target) if target.exists() else None
        if current_hash != operation["old_hash"]:
            raise SafetyError(f"Destination changed during sync: {target}")
        if current_hash is not None:
            os.replace(target, root / "old" / str(index))
            _sync_directory(root / "old")
        if operation["new_hash"] is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(root / "new" / str(index), target)
        _sync_directory(target.parent)
        written.append(target)
        if progress:
            progress("Writing repository", len(written), len(operations))
    return written


def apply_plans(
    plans: list[Plan], repository: Path, progress: Progress | None = None
) -> list[Path]:
    """Stage all selected instances, then apply them as one recoverable transaction."""
    assert_plain_directory(repository)
    recover_transaction(repository)
    if not any(plan.changed for plan in plans):
        return []
    total_bytes = sum(
        change.source.stat().st_size
        for plan in plans
        for change in plan.changes
        if change.source is not None and change.kind != "delete"
    )
    display_total = max(total_bytes, 1)
    copied = 0

    def on_chunk(size: int) -> None:
        nonlocal copied
        copied += size
        if progress:
            progress("Loading files", copied, display_total)

    if progress:
        progress("Loading files", 0, display_total)
    root = repository / TRANSACTION
    try:
        root.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise SafetyError(f"Transaction directory already exists: {root}") from exc
    try:
        _write_journal(root, "staging", [])
        (root / "new").mkdir(mode=0o700)
        (root / "old").mkdir(mode=0o700)
        operations = _prepare(plans, repository, root, on_chunk)
        if progress:
            progress("Loading files", display_total, display_total)
        _write_journal(root, "ready", operations)
    except BaseException:
        if (root / JOURNAL).exists():
            _cleanup(root)
        elif not any(root.iterdir()):
            root.rmdir()
        raise
    try:
        written = _apply(repository, root, operations, progress)
        _write_journal(root, "committed", operations)
    except BaseException:
        try:
            recover_transaction(repository)
        except (OSError, SafetyError) as exc:
            raise SafetyError(f"Automatic rollback failed; inspect {root}: {exc}") from exc
        raise
    _cleanup(root)
    return written
