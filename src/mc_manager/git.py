from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .paths import state_dir


class GitError(RuntimeError):
    pass


@contextmanager
def repository_lock(repo: Path):
    """Prevent concurrent mc_manager runs from changing the same repository."""
    directory = state_dir() / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest() + ".lock"
    descriptor = os.open(directory / name, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise GitError("Another mc_manager process is using this repository") from exc
        else:
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise GitError("Another mc_manager process is using this repository") from exc
        try:
            yield
        finally:
            if sys.platform == "win32":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def run(repo: Path, *args: str) -> str:
    try:
        process = subprocess.run(
            ["git", "-C", str(repo), *args],
            text=True,
            capture_output=True,
            check=False,
            timeout=90,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError("Git operation timed out") from exc
    if process.returncode:
        raise GitError(process.stderr.strip() or f"git {' '.join(args)} failed")
    return process.stdout.strip()


def verify_repository(repo: Path) -> None:
    if not repo.is_dir() or repo.is_symlink():
        raise GitError(f"Not a regular directory: {repo}")
    top = Path(run(repo, "rev-parse", "--show-toplevel")).resolve()
    if top != repo.resolve():
        raise GitError(f"Choose Git repository root, not a subdirectory: {top}")


def dirty(repo: Path) -> bool:
    return bool(run(repo, "status", "--porcelain"))


def has_origin(repo: Path) -> bool:
    return "origin" in run(repo, "remote").splitlines()


def ensure_commit_identity(repo: Path) -> None:
    try:
        run(repo, "var", "GIT_AUTHOR_IDENT")
        run(repo, "var", "GIT_COMMITTER_IDENT")
    except GitError as exc:
        raise GitError(
            "Git author identity is missing. Set user.name and user.email in the chosen repository "
            "before synchronizing, or set git_commit = false in mc_manager config."
        ) from exc


def pull(repo: Path) -> None:
    if dirty(repo):
        raise GitError("Repository has unsaved changes; pull skipped")
    if has_origin(repo):
        run(repo, "pull", "--ff-only")


def validate_remote_url(url: str) -> None:
    if url.startswith(("https://", "ssh://")):
        parts = urlsplit(url)
        if not parts.hostname or not parts.path or parts.password or parts.query or parts.fragment:
            raise GitError("Use an HTTPS or SSH Git URL without embedded credentials or query")
        if parts.scheme == "https" and parts.username:
            raise GitError("Use Git credential storage, not credentials in the URL")
    elif not re.fullmatch(r"git@[^:\s]+:[^\s]+", url):
        raise GitError("Use an HTTPS or SSH Git URL without embedded credentials")


def clone(url: str, destination: Path) -> None:
    if destination.exists():
        raise GitError(f"Clone destination already exists: {destination}")
    validate_remote_url(url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        process = subprocess.run(
            ["git", "clone", "--", url, str(destination)],
            text=True,
            capture_output=True,
            check=False,
            timeout=90,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError("Git clone timed out") from exc
    if process.returncode:
        raise GitError(process.stderr.strip() or "git clone failed")
    verify_repository(destination)


def stage(repo: Path, paths: list[Path]) -> bool:
    relative = sorted({p.relative_to(repo).as_posix() for p in paths})
    if not relative:
        return False
    run(repo, "add", "-A", "--", *relative)
    return bool(run(repo, "diff", "--cached", "--name-only"))


def commit(repo: Path, message: str) -> None:
    run(repo, "commit", "-m", message)


def push(repo: Path) -> None:
    if not has_origin(repo):
        raise GitError("No origin remote configured")
    run(repo, "push", "origin", "HEAD")
