from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .config import Config, config_path, data_path, load, save
from .discovery import DiscoveryReport, discover_report
from .git import (
    GitError,
    clone,
    commit,
    dirty,
    ensure_commit_identity,
    has_origin,
    pull,
    push,
    stage,
    verify_repository,
)
from .models import MinecraftInstance, Plan
from .safety import SafetyError
from .sync import apply_plan, destination_for, plan_instance

NAMES = {
    "prism": "Prism Launcher",
    "multimc": "MultiMC",
    "modrinth": "Modrinth App",
    "atlauncher": "ATLauncher",
    "curseforge": "CurseForge",
    "gdlauncher": "GDLauncher",
    "vanilla": "Minecraft Launcher",
    "legacy": "Legacy Launcher",
    "tlauncher": "TLauncher",
    "sklauncher": "SKLauncher",
    "generic": "Generic Minecraft installation",
}


def state_path() -> Path:
    return (
        Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        / "mc_manager/seen.json"
    )


def ask(message: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"{message} [{hint}] ").strip().lower()
    return default if not answer else answer in {"y", "yes"}


def show_scan(report: DiscoveryReport) -> None:
    print("Minecraft Manager\n\nScanning clients...\n")
    for client in (
        "prism",
        "legacy",
        "tlauncher",
        "sklauncher",
        "vanilla",
        "multimc",
        "modrinth",
        "atlauncher",
        "curseforge",
        "gdlauncher",
        "generic",
    ):
        count = report.clients.get(client, 0)
        if client == "generic" and not any(i.client == "generic" for i in report.instances):
            continue
        if client in report.installed:
            mark = "?" if client == "generic" else "✓"
            print(
                f"{mark} {NAMES[client]}: {count} candidate(s)"
                if client == "generic"
                else f"{mark} {NAMES[client]}: {count} installation(s)"
            )
            for instance in report.instances:
                if client not in (instance.launchers or (instance.client,)):
                    continue
                if client != instance.client:
                    print(f"  └─ {instance.name} — shared with {NAMES[instance.client]}")
                else:
                    version = instance.minecraft.value or "unknown"
                    loader = instance.loader.value or "unknown"
                    loader_v = (
                        f" {instance.loader_version.value}" if instance.loader_version.value else ""
                    )
                    detail = f"{version}, {loader}{loader_v} [{instance.minecraft.confidence}]"
                    print(f"  └─ {instance.name}: {detail}")
        elif client in {"prism", "legacy", "tlauncher", "sklauncher"}:
            print(f"✗ {NAMES[client]}: not found")
    known = report.installed - {"generic"}
    launcher_word = "launcher" if len(known) == 1 else "launchers"
    installation_word = "installation" if len(report.instances) == 1 else "installations"
    summary = f"{len(known)} {launcher_word} detected; "
    summary += f"{len(report.instances)} unique {installation_word}"
    print(f"\n{summary}")
    generic_count = report.clients.get("generic", 0)
    print(f"Discovery: generic {generic_count}; duplicates merged {report.duplicates_merged}")


def setup(report: DiscoveryReport) -> Config:
    print("\nWelcome to mc_manager. Configure a Git repository.")
    print("Found instances:")
    for number, instance in enumerate(report.instances, 1):
        print(
            f"  {number}. {NAMES[instance.client]} ({instance.client}) / "
            f"{instance.name} ({instance.game_dir})"
        )
    raw = input("Git repository path or SSH/HTTPS URL: ").strip()
    if not raw:
        raise GitError("A Git repository is required")
    config = Config()
    if raw.startswith(("git@", "https://", "ssh://")):
        default = data_path() / "repository"
        local = input(f"Local clone directory [{default}]: ").strip()
        destination = Path(local).expanduser() if local else default
        clone(raw, destination)
        config.repository, config.remote = destination, raw
    else:
        repo = Path(raw).expanduser()
        verify_repository(repo)
        config.repository = repo.resolve()
    excluded = input("Instance numbers to exclude (comma separated; Enter for all): ").strip()
    if excluded:
        for part in excluded.split(","):
            if part.strip().isdigit() and 1 <= int(part.strip()) <= len(report.instances):
                config.excluded_instances.append(report.instances[int(part.strip()) - 1].key)
    print("Launcher IDs: " + ", ".join(sorted(report.installed - {"generic"})))
    clients = input("Launcher IDs to exclude (comma separated; Enter for all): ").strip()
    config.excluded_clients = [s.strip().lower() for s in clients.split(",") if s.strip()]
    save(config)
    print(f"Saved {config_path()}")
    return config


def eligible(instance: MinecraftInstance, config: Config) -> bool:
    return instance.key not in config.excluded_instances and any(
        client not in config.excluded_clients
        for client in (instance.launchers or (instance.client,))
    )


def make_plans(report: DiscoveryReport, config: Config, selected: str | None = None) -> list[Plan]:
    if config.repository is None:
        raise GitError("Repository is not configured; run mc_manager in a terminal to set it up")
    repo = config.repository
    verify_repository(repo)
    instances = [i for i in report.instances if eligible(i, config)]
    if selected:
        instances = [
            i
            for i in instances
            if i.name.casefold() == selected.casefold() or selected.casefold() in i.name.casefold()
        ]
        if not instances:
            raise GitError(f"No enabled instance matching {selected!r}")
    used: set[str] = set()
    plans = []
    for instance in instances:
        relative = destination_for(instance, config, used)
        plans.append(plan_instance(instance, repo, relative, config.ignores))
    return plans


def show_plans(plans: list[Plan]) -> None:
    changed = [p for p in plans if p.changed]
    print(f"\n{len(changed)} changed; {len(plans) - len(changed)} unchanged")
    for plan in changed:
        print(f"\n{NAMES.get(plan.instance.client, plan.instance.client)} / {plan.instance.name}")
        if not plan.changes:
            print("  metadata changed")
        for change in plan.changes:
            prefix = {"add": "+", "modify": "~", "delete": "-"}[change.kind]
            print(f"  {prefix} {change.path}")


def _seen(report: DiscoveryReport) -> None:
    path = state_path()
    try:
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        old = {}
    old_clients = set(old.get("clients", []))
    old_instances = set(old.get("instances", []))
    current_clients = report.installed - {"generic"}
    for client in sorted(current_clients - old_clients):
        print(f"New Minecraft client detected: {NAMES.get(client, client)}")
    for instance in report.instances:
        if instance.key not in old_instances:
            launcher = NAMES.get(instance.client, instance.client)
            print(f"New Minecraft instance: {launcher} / {instance.name}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "clients": sorted(current_clients),
                    "instances": [i.key for i in report.instances],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        logging.warning("Could not save discovery state: %s", exc)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mc_manager",
        description="Discover Minecraft installations and sync selected content to Git",
    )
    p.add_argument("--version", action="version", version=f"mc_manager {__version__}")
    p.add_argument("--verbose", action="store_true")
    sub = p.add_subparsers(dest="command")
    scan = sub.add_parser("scan", help="Discover launchers and instances")
    scan.add_argument(
        "--force", action="store_true", help="Rescan now (scanning always reads live metadata)"
    )
    scan.add_argument(
        "--debug", action="store_true", help="Show candidate scores and deduplication"
    )
    sub.add_parser("status", help="Show pending repository changes")
    sync = sub.add_parser("sync", help="Synchronize enabled instances")
    sync.add_argument("instance", nargs="?", help="Instance name or substring")
    sync.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    sub.add_parser("clients", help="List launcher status")
    sub.add_parser("instances", help="List unique instances")
    conf = sub.add_parser("config", help="Show configuration")
    conf.add_argument("--setup", action="store_true", help="Run setup wizard")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s: %(message)s"
    )
    try:
        if args.command == "config" and not args.setup:
            config = load()
            print(f"Config: {config_path()}\nRepository: {config.repository or 'not configured'}")
            return 0
        config = load()
        report = discover_report(
            extra_roots=config.extra_roots, debug=getattr(args, "debug", False)
        )
        show_scan(report)
        if getattr(args, "debug", False):
            for line in report.debug:
                print(line)
        if args.command in {"scan", "clients", "instances"}:
            _seen(report)
            return 0
        if (
            args.command == "config"
            and args.setup
            or config.repository is None
            and args.command is None
        ):
            if not sys.stdin.isatty():
                print("Setup requires an interactive terminal. Run mc_manager there.")
                return 0
            config = setup(report)
        if config.repository is None:
            raise GitError(
                "Repository is not configured. Run mc_manager interactively to set it up."
            )
        dry_run = args.command == "sync" and args.dry_run
        if args.command != "status" and not dry_run:
            verify_repository(config.repository)
            if dirty(config.repository):
                raise GitError("Repository has uncommitted changes; sync aborted")
            if config.git_pull and has_origin(config.repository):
                pull(config.repository)
        plans = make_plans(report, config, args.instance if args.command == "sync" else None)
        show_plans(plans)
        if args.command == "status" or dry_run or not any(p.changed for p in plans):
            _seen(report)
            return 0
        if config.ask_before_sync and not (sys.stdin.isatty() and ask("Synchronize changes?")):
            print("Synchronization skipped")
            _seen(report)
            return 0
        if config.git_commit:
            ensure_commit_identity(config.repository)
        changed_paths: list[Path] = []
        for plan in plans:
            if plan.changed:
                changed_paths.extend(apply_plan(plan, config.repository))
                print(f"✓ {plan.instance.name}")
        print("Synchronization complete.")
        save(config)
        if (
            config.git_commit
            and changed_paths
            and (not sys.stdin.isatty() or ask("Create Git commit?"))
        ):
            if stage(config.repository, changed_paths):
                commit(
                    config.repository,
                    f"mc_manager: sync {len([p for p in plans if p.changed])} instance(s)",
                )
                print("Git commit created.")
                if (
                    has_origin(config.repository)
                    and sys.stdin.isatty()
                    and ask("Push to origin?", config.git_push)
                ):
                    push(config.repository)
                    print("Pushed to origin.")
        _seen(report)
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (GitError, SafetyError, OSError, ValueError) as exc:
        sys.stdout.flush()
        print(f"mc_manager: {exc}", file=sys.stderr)
        return 1
