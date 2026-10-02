"""User data and Minecraft locations without third-party dependencies."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _environment_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def roaming_dir(home: Path | None = None) -> Path:
    home = home or Path.home()
    return _environment_path("APPDATA") or home / "AppData/Roaming"


def local_dir(home: Path | None = None) -> Path:
    home = home or Path.home()
    return _environment_path("LOCALAPPDATA") or home / "AppData/Local"


def minecraft_dir(home: Path | None = None) -> Path:
    home = home or Path.home()
    if sys.platform == "win32":
        return roaming_dir(home) / ".minecraft"
    if sys.platform == "darwin":
        return home / "Library/Application Support/minecraft"
    return home / ".minecraft"


def config_dir(home: Path | None = None) -> Path:
    home = home or Path.home()
    override = _environment_path("XDG_CONFIG_HOME")
    if override:
        return override / "mc_manager"
    if sys.platform == "win32":
        return roaming_dir(home) / "mc_manager"
    if sys.platform == "darwin":
        return home / "Library/Application Support/mc_manager"
    return home / ".config/mc_manager"


def data_dir(home: Path | None = None) -> Path:
    home = home or Path.home()
    override = _environment_path("XDG_DATA_HOME")
    if override:
        return override / "mc_manager"
    if sys.platform == "win32":
        return local_dir(home) / "mc_manager"
    if sys.platform == "darwin":
        return home / "Library/Application Support/mc_manager"
    return home / ".local/share/mc_manager"


def state_dir(home: Path | None = None) -> Path:
    home = home or Path.home()
    override = _environment_path("XDG_STATE_HOME")
    if override:
        return override / "mc_manager"
    if sys.platform == "win32":
        return local_dir(home) / "mc_manager"
    if sys.platform == "darwin":
        return home / "Library/Application Support/mc_manager"
    return home / ".local/state/mc_manager"
