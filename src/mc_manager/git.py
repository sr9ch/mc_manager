from __future__ import annotations

import re
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


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


def pull(repo: Path) -> None:
    if dirty(repo):
        raise GitError("Repository has unsaved changes; pull skipped")
    if has_origin(repo):
        run(repo, "pull", "--ff-only")


def clone(url: str, destination: Path) -> None:
    if destination.exists():
        raise GitError(f"Clone destination already exists: {destination}")
    if not (
        re.match(r"^(https://|ssh://|git@[^:]+:).+", url) and not re.match(r"^https?://[^/]*@", url)
    ):
        raise GitError("Use an HTTPS or SSH Git URL without embedded credentials")
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
