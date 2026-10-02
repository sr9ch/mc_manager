from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from pathlib import Path


class SafetyError(RuntimeError):
    """Unsafe source, destination, or repository state."""


CONTENT_DIRS = (
    "mods",
    "shaderpacks",
    "resourcepacks",
    "config",
    "defaultconfigs",
    "kubejs",
    "scripts",
    "patchouli_books",
    "datapacks",
)
BLOCKED_PARTS = {
    "logs",
    "crash-reports",
    "screenshots",
    "cache",
    "caches",
    "assets",
    "libraries",
    "natives",
    "runtime",
    "accounts",
    "credentials",
    "tokens",
    ".git",
    "temp",
    "tmp",
    "backups",
    "saves",
    "downloads",
}
SENSITIVE_NAME = re.compile(
    r"(^|[._-])(token|secret|credential|password|passwd|session|account|auth|keychain|oauth|private.?key|apikey|accesstoken|refreshtoken|clientsecret)([._-]|$)",
    re.IGNORECASE,
)
SENSITIVE_CONTENT = re.compile(
    rb"-----BEGIN (?:[A-Z ]*PRIVATE KEY|OPENSSH PRIVATE KEY)-----|"
    rb'(?i:["\']?(?:access[_-]?token|refresh[_-]?token|client[_-]?secret|api[_-]?key|password|passwd|session[_-]?id|authorization)["\']?\s*[:=]\s*["\']?[^\s"\']{6,})'
)
MAX_FILE = 100 * 1024 * 1024


def slug(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    clean = re.sub(r"[\W_]+", "-", normalized, flags=re.UNICODE).strip("-")[:60].strip("-")
    if clean in {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }:
        clean += "-instance"
    return clean or "instance"


def world_slug(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    readable = re.sub(r"[\W_]+", "-", normalized, flags=re.UNICODE).strip("-")[:60]
    return f"{readable or 'world'}-{hashlib.sha256(text.encode()).hexdigest()[:8]}"


def safe_relative(path: str) -> Path:
    p = Path(path)
    if (
        "\\" in path
        or p.is_absolute()
        or not p.parts
        or any(part in (".", "..", "") for part in p.parts)
    ):
        raise SafetyError(f"Unsafe relative path: {path}")
    return p


def assert_plain_directory(path: Path, boundary: Path | None = None) -> None:
    if path.is_symlink() or not path.is_dir():
        raise SafetyError(f"Expected a regular directory: {path}")
    if boundary is not None and not path.resolve().is_relative_to(boundary.resolve()):
        raise SafetyError(f"Directory escapes allowed root: {path}")


def assert_destination(root: Path, relative: Path) -> Path:
    safe_relative(relative.as_posix())
    assert_plain_directory(root)
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise SafetyError(f"Symlink in destination: {current}")
        if current.exists() and not current.is_dir():
            raise SafetyError(f"Non-directory in destination: {current}")
    target = root / relative
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise SafetyError(f"Unsafe destination: {target}")
    return target


def allowed_relative(relative: Path) -> bool:
    parts = relative.parts
    return bool(parts) and all(
        p.lower() not in BLOCKED_PARTS
        and not p.startswith(".")
        and not SENSITIVE_NAME.search(p)
        and not p.endswith(("~", ".bak", ".tmp", ".log"))
        for p in parts
    )


def safe_content(path: Path, category: str | None = None) -> bool:
    if path.stat().st_size > MAX_FILE:
        return False
    # Config archives are opaque and may contain private data. Allow archives only
    # in directories intended for mods and packs.
    if path.suffix.lower() in {".jar", ".zip"}:
        return category in {"mods", "resourcepacks", "shaderpacks", "datapacks"}
    if path.suffix.lower() in {".png", ".jpg", ".ogg", ".webp"}:
        return True
    if path.stat().st_size > 4 * 1024 * 1024:
        return False
    with path.open("rb") as stream:
        return not SENSITIVE_CONTENT.search(stream.read())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
