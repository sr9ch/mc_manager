from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
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
    run,
    stage,
    validate_remote_url,
    verify_repository,
)
from .models import MinecraftInstance, Plan
from .safety import SafetyError
from .scan_state import report_changes
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
    order = (
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
    )
    known = report.installed - {"generic"}
    launcher_word = "launcher" if len(known) == 1 else "launchers"
    installation_word = "installation" if len(report.instances) == 1 else "installations"
    summary = f"{len(known)} {launcher_word} detected; "
    summary += f"{len(report.instances)} unique {installation_word}"
    print(f"\n{summary}\n\nLaunchers")
    for client in order:
        if client in known:
            count = report.clients.get(client, 0)
            detail = (
                "installed, no instances"
                if count == 0
                else f"{count} {'installation' if count == 1 else 'installations'}"
            )
            print(f"  ✓ {NAMES[client]} — {detail}")
    if not known:
        print("  None detected")
    print("\nInstallations")
    for instance in report.instances:
        launchers = instance.launchers or (instance.client,)
        names = ", ".join(NAMES.get(client, client) for client in launchers)
        version = instance.minecraft.value or "unknown"
        loader = instance.loader.value or "unknown"
        loader_version = (
            f" {instance.loader_version.value}" if instance.loader_version.value else ""
        )
        print(f"  • {instance.name} — {names}")
        print(
            f"    Minecraft {version} · {loader}{loader_version} [{instance.minecraft.confidence}]"
        )
    if not report.instances:
        print("  None detected")


def show_discovery_details(report: DiscoveryReport) -> None:
    print("\nDiscovery details")
    missing = [
        NAMES[client]
        for client in ("prism", "legacy", "tlauncher", "sklauncher")
        if client not in report.installed
    ]
    if missing:
        print("  Not detected: " + ", ".join(missing))
    print(
        f"  Generic candidates: {report.clients.get('generic', 0)}; "
        f"duplicates merged {report.duplicates_merged}"
    )
    for instance in report.instances:
        print(f"  {instance.name}: {instance.game_dir}")


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
    config = load()
    if raw.startswith(("git@", "https://", "ssh://")):
        default = (
            config.repository
            if config.remote == raw and config.repository
            else data_path() / "repository"
        )
        local = input(f"Local clone directory [{default}]: ").strip()
        destination = Path(local).expanduser() if local else default
        if destination.exists():
            verify_repository(destination)
            if run(destination, "config", "--get", "remote.origin.url") != raw:
                raise GitError(f"Clone destination has a different origin URL: {destination}")
        else:
            clone(raw, destination)
        config.repository, config.remote = destination, raw
    elif "://" in raw or raw.startswith("git@"):
        validate_remote_url(raw)
        raise GitError("Use an HTTPS or SSH Git URL")
    else:
        repo = Path(raw).expanduser()
        verify_repository(repo)
        previous_repo = config.repository
        previous_remote = config.remote
        config.repository = repo.resolve()
        config.remote = (
            previous_remote
            if previous_repo and previous_repo.resolve() == config.repository
            else None
        )
    excluded = input("Instance numbers to exclude (comma separated; Enter for all): ").strip()
    config.excluded_instances = []
    if excluded:
        for part in excluded.split(","):
            value = part.strip()
            if not value.isdigit() or not 1 <= int(value) <= len(report.instances):
                raise GitError(f"Invalid instance number: {value or '(empty)'}")
            config.excluded_instances.append(report.instances[int(value) - 1].key)
        config.excluded_instances = list(dict.fromkeys(config.excluded_instances))
    available_clients = report.installed - {"generic"}
    print("Launcher IDs: " + (", ".join(sorted(available_clients)) or "none"))
    clients = input("Launcher IDs to exclude (comma separated; Enter for all): ").strip()
    config.excluded_clients = [s.strip().lower() for s in clients.split(",") if s.strip()]
    unknown = set(config.excluded_clients) - available_clients
    if unknown:
        raise GitError("Unknown launcher ID: " + ", ".join(sorted(unknown)))
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


def show_plans(plans: list[Plan], verbose: bool = False) -> None:
    changed = [p for p in plans if p.changed]
    print(f"\nRepository\n  {len(changed)} changed; {len(plans) - len(changed)} unchanged")
    for plan in changed:
        print(f"\n{NAMES.get(plan.instance.client, plan.instance.client)} / {plan.instance.name}")
        if not plan.changes:
            print("  metadata changed")
            continue
        kinds = Counter(change.kind for change in plan.changes)
        categories = Counter(change.path.split("/", 1)[0] for change in plan.changes)
        print(f"  +{kinds['add']} added  ~{kinds['modify']} updated  -{kinds['delete']} removed")
        print(
            "  "
            + ", ".join(
                f"{category} {count}"
                for category, count in sorted(
                    categories.items(), key=lambda item: (-item[1], item[0])
                )
            )
        )
        ordered = sorted(
            plan.changes,
            key=lambda change: ({"delete": 0, "modify": 1, "add": 2}[change.kind], change.path),
        )
        shown = ordered if verbose else ordered[:12]
        for change in shown:
            prefix = {"add": "+", "modify": "~", "delete": "-"}[change.kind]
            print(f"  {prefix} {change.path}")
        if len(shown) < len(ordered):
            print(f"  ... {len(ordered) - len(shown)} more paths (use --verbose for all)")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mc_manager",
        description="Discover Minecraft installations and sync selected content to Git",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  mc_manager\n  mc_manager scan\n  mc_manager sync --dry-run",
    )
    p.add_argument("--version", action="version", version=f"mc_manager {__version__}")
    p.add_argument(
        "--verbose", action="store_true", help="Show every changed path and scan diagnostics"
    )
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
        first_launch = not state_path().exists()
        print(f"Minecraft Manager {__version__}\nScanning local installations...", flush=True)
        report = discover_report(
            extra_roots=config.extra_roots,
            debug=getattr(args, "debug", False),
            excluded_roots=[config.repository] if config.repository else [],
        )
        show_scan(report)
        if args.verbose or getattr(args, "debug", False):
            show_discovery_details(report)
        if getattr(args, "debug", False):
            print("\nCandidate details")
            for line in report.debug:
                print(f"  {line}")
        print("\nChanges since last scan")
        for line in report_changes(report, state_path(), config.ignores, NAMES):
            print(f"  {line}")
        if args.command in {"scan", "clients", "instances"}:
            return 0
        if args.command == "config" and args.setup:
            if not sys.stdin.isatty():
                print("Setup requires an interactive terminal. Run mc_manager there.")
                return 1
            setup(report)
            return 0
        if config.repository is None:
            if args.command is None:
                if (
                    first_launch
                    and sys.stdin.isatty()
                    and ask("Connect a Git repository for synchronization?", False)
                ):
                    config = setup(report)
                else:
                    print("\nGit repository not configured.")
                    print("Run mc_manager config --setup to enable sync.")
                    return 0
            else:
                raise GitError("Repository is not configured. Run mc_manager config --setup.")
        dry_run = args.command == "sync" and args.dry_run
        if args.command != "status" and not dry_run:
            verify_repository(config.repository)
            if dirty(config.repository):
                raise GitError("Repository has uncommitted changes; sync aborted")
            if config.git_pull and has_origin(config.repository):
                pull(config.repository)
        plans = make_plans(report, config, args.instance if args.command == "sync" else None)
        show_plans(plans, verbose=args.verbose)
        if args.command == "status" or dry_run or not any(p.changed for p in plans):
            return 0
        if config.ask_before_sync and not (sys.stdin.isatty() and ask("Synchronize changes?")):
            print("Synchronization skipped")
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
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (GitError, SafetyError, OSError, ValueError) as exc:
        sys.stdout.flush()
        print(f"mc_manager: {exc}", file=sys.stderr)
        return 1
