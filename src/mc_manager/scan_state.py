from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .discovery import DiscoveryReport
from .safety import SafetyError
from .sync import source_files

LOG = logging.getLogger(__name__)
SCHEMA = 3
DETAIL_LIMIT = 12


def _load(path: Path) -> tuple[dict, bool]:
    if not path.exists():
        return {}, True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (data if isinstance(data, dict) else {}), False
    except (OSError, ValueError) as exc:
        LOG.warning("Could not read previous scan: %s", exc)
        return {}, True


def _snapshot(report: DiscoveryReport, ignores: list[str], old: dict) -> dict:
    old_instances = old.get("instances", {})
    if not isinstance(old_instances, dict):
        old_instances = {}
    instances = {}
    for instance in report.instances:
        previous = old_instances.get(instance.key, {})
        try:
            files = {}
            for name, path in sorted(source_files(instance, ignores).items()):
                stat = path.stat()
                files[name] = [stat.st_size, stat.st_mtime_ns]
        except (OSError, SafetyError) as exc:
            LOG.warning("Could not inspect %s: %s", instance.game_dir, exc)
            files = previous.get("files", {}) if isinstance(previous, dict) else {}
        instances[instance.key] = {
            "name": instance.name,
            "client": instance.client,
            "minecraft": instance.minecraft.value,
            "loader": instance.loader.value,
            "loader_version": instance.loader_version.value,
            "files": files,
        }
    return {
        "schema": SCHEMA,
        "clients": sorted(report.installed - {"generic"}),
        "instances": instances,
    }


def _file_changes(name: str, before: dict, after: dict) -> list[str]:
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    modified = sorted(key for key in before.keys() & after.keys() if before[key] != after[key])
    if not (added or modified or removed):
        return []
    lines = [f"Content changed in {name}: +{len(added)} ~{len(modified)} -{len(removed)} files"]
    details = [*(f"  + {path}" for path in added)]
    details.extend(f"  ~ {path}" for path in modified)
    details.extend(f"  - {path}" for path in removed)
    lines.extend(details[:DETAIL_LIMIT])
    if len(details) > DETAIL_LIMIT:
        lines.append(f"  ... and {len(details) - DETAIL_LIMIT} more")
    return lines


def _changes(
    old: dict, current: dict, first_scan: bool, launcher_names: Mapping[str, str]
) -> list[str]:
    if first_scan:
        return ["First scan saved as baseline."]
    lines = []
    saved_clients = old.get("clients", [])
    if not isinstance(saved_clients, list):
        saved_clients = []
    old_clients = {client for client in saved_clients if isinstance(client, str)}
    current_clients = set(current["clients"])
    for client in sorted(current_clients - old_clients):
        lines.append(f"New launcher detected: {launcher_names.get(client, client)}")
    for client in sorted(old_clients - current_clients):
        lines.append(f"Launcher no longer detected: {launcher_names.get(client, client)}")

    previous = old.get("instances", {})
    if isinstance(previous, list):
        # Upgrade the original state format without reporting every known instance as new.
        previous = {key: {} for key in previous if isinstance(key, str)}
    if not isinstance(previous, dict):
        previous = {}
    now = current["instances"]
    for key in sorted(now.keys() - previous.keys()):
        item = now[key]
        version = item["minecraft"] or "unknown"
        launcher = launcher_names.get(item["client"], item["client"])
        lines.append(f"New instance: {launcher} / {item['name']} (Minecraft {version})")
    for key in sorted(previous.keys() - now.keys()):
        item = previous[key]
        name = item.get("name", key) if isinstance(item, dict) else key
        lines.append(f"Instance no longer detected: {name}")
    for key in sorted(now.keys() & previous.keys()):
        before, after = previous[key], now[key]
        if not isinstance(before, dict):
            continue
        name = after["name"]
        if before.get("name") and before["name"] != name:
            lines.append(f"Instance renamed: {before['name']} → {name}")
        for field, label in (
            ("minecraft", "Minecraft version"),
            ("loader", "Loader"),
            ("loader_version", "Loader version"),
        ):
            if field in before and before[field] != after[field]:
                lines.append(
                    f"{label} changed in {name}: "
                    f"{before[field] or 'unknown'} → {after[field] or 'unknown'}"
                )
        old_files = before.get("files")
        if old.get("schema") == SCHEMA and isinstance(old_files, dict):
            lines.extend(_file_changes(name, old_files, after["files"]))
    return lines or ["No changes since last scan."]


def report_changes(
    report: DiscoveryReport,
    path: Path,
    ignores: list[str] | None = None,
    launcher_names: Mapping[str, str] | None = None,
) -> list[str]:
    old, first_scan = _load(path)
    current = _snapshot(report, ignores or [], old)
    lines = _changes(old, current, first_scan, launcher_names or {})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=".seen-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(current, stream, indent=2, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    except OSError as exc:
        LOG.warning("Could not save discovery state: %s", exc)
        if first_scan:
            return ["First scan complete; baseline could not be saved."]
    return lines
