from __future__ import annotations

import argparse
import json
import logging
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
    repository_lock,
    run,
    stage,
    validate_remote_url,
    verify_repository,
)
from .models import MinecraftInstance, Plan
from .paths import state_dir
from .progress import TransferProgress, activity
from .safety import SafetyError, assert_destination, safe_relative
from .scan_state import report_changes
from .sync import (
    MANIFEST,
    _manifest,
    apply_plans,
    destination_for,
    plan_instance,
    recover_transaction,
)

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
    return state_dir() / "seen.json"


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
            with activity("Cloning repository"):
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
    save(config)
    print(f"Saved {config_path()}")
    return config


def eligible(instance: MinecraftInstance, config: Config) -> bool:
    return instance.key not in config.excluded_instances and any(
        client not in config.excluded_clients
        for client in (instance.launchers or (instance.client,))
    )


def selected_instances(
    report: DiscoveryReport, config: Config, selected: str | None = None
) -> list[MinecraftInstance]:
    instances = [i for i in report.instances if eligible(i, config)]
    if selected:
        instances = [
            i
            for i in instances
            if i.name.casefold() == selected.casefold() or selected.casefold() in i.name.casefold()
        ]
        if not instances:
            raise GitError(f"No enabled instance matching {selected!r}")
    return instances


def decision_key(instance: MinecraftInstance) -> str:
    """A new Minecraft or loader version needs a fresh decision for its game directory."""
    return json.dumps(
        [
            instance.key,
            instance.minecraft.value,
            instance.loader.value,
            instance.loader_version.value,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def previously_synced(instance: MinecraftInstance, config: Config) -> bool:
    if config.repository is None:
        return False
    saved = config.destinations.get(instance.key)
    if not saved:
        return False
    relative = safe_relative(saved)
    if relative.parts[:1] != ("minecraft",) or len(relative.parts) != 3:
        raise SafetyError(f"Invalid managed destination: {relative}")
    manifest_path = assert_destination(config.repository, relative / MANIFEST)
    old = _manifest(manifest_path).get("instance", {})
    if not isinstance(old, dict):
        return False
    minecraft = old.get("minecraft", {})
    loader = old.get("loader", {})
    return (
        isinstance(minecraft, dict)
        and isinstance(loader, dict)
        and minecraft.get("version") == instance.minecraft.value
        and loader.get("type") == instance.loader.value
        and loader.get("version") == instance.loader_version.value
    )


def resolve_new_decisions(
    instances: list[MinecraftInstance], config: Config, args: argparse.Namespace
) -> None:
    pending = []
    migrated = False
    reconsider = getattr(args, "reconsider", False)
    for instance in instances:
        key = decision_key(instance)
        if reconsider:
            config.decisions.pop(key, None)
        if key in config.decisions:
            continue
        if not reconsider and previously_synced(instance, config):
            config.decisions[key] = True
            migrated = True
        else:
            pending.append((instance, key))
    if migrated:
        save(config)
    if not pending:
        return
    if getattr(args, "all_new", False) or getattr(args, "skip_new", False):
        accepted = args.all_new
        for _, key in pending:
            config.decisions[key] = accepted
        save(config)
        print(f"New builds: {'all approved' if accepted else 'all skipped'} ({len(pending)}).")
        return
    if not sys.stdin.isatty():
        print(f"{len(pending)} new build(s) await a decision.")
        print("Run in a terminal or use --all-new/--skip-new.")
        return
    print(f"\nНайдены новые сборки: {len(pending)}")
    for number, (instance, _) in enumerate(pending, 1):
        version = instance.minecraft.value or "версия неизвестна"
        loader = instance.loader.value or "загрузчик неизвестен"
        print(f"  {number}. {instance.name} — Minecraft {version}, {loader}")
    while True:
        choice = input("Новые сборки: [a] все / [n] ничего / [e] по одной: ").strip().lower()
        if choice in {"a", "all", "в", "все"}:
            answer = True
            break
        if choice in {"n", "none", "н", "ничего"}:
            answer = False
            break
        if choice in {"e", "each", "п", "по одной", ""}:
            answer = None
            break
        print("Введите a, n или e.")
    if answer is not None:
        for _, key in pending:
            config.decisions[key] = answer
        save(config)
        return
    for instance, key in pending:
        while True:
            label = f"{instance.name} (Minecraft {instance.minecraft.value or '?'})"
            raw = input(f"Синхронизировать {label}? [y/n] ").strip().lower()
            if raw in {"y", "yes", "д", "да", "n", "no", "н", "нет"}:
                config.decisions[key] = raw in {"y", "yes", "д", "да"}
                save(config)
                break
            print("Введите y или n.")


def make_plans(
    report: DiscoveryReport,
    config: Config,
    selected: str | None = None,
    approved_only: bool = False,
) -> list[Plan]:
    if config.repository is None:
        raise GitError("Repository is not configured; run mc_manager in a terminal to set it up")
    repo = config.repository
    verify_repository(repo)
    instances = selected_instances(report, config, selected)
    if approved_only:
        instances = [i for i in instances if config.decisions.get(decision_key(i)) is True]
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
    new = sync.add_mutually_exclusive_group()
    new.add_argument("--all-new", action="store_true", help="Approve every newly detected build")
    new.add_argument("--skip-new", action="store_true", help="Decline every newly detected build")
    sync.add_argument(
        "--reconsider", action="store_true", help="Ask again for current build versions"
    )
    sub.add_parser("clients", help="List launcher status")
    sub.add_parser("instances", help="List unique instances")
    conf = sub.add_parser("config", help="Show configuration")
    conf.add_argument("--setup", action="store_true", help="Run setup wizard")
    return p


def synchronize(report: DiscoveryReport, config: Config, args: argparse.Namespace) -> int:
    assert config.repository is not None
    repository = config.repository
    dry_run = args.command == "sync" and args.dry_run
    if dry_run and (args.all_new or args.skip_new or args.reconsider):
        raise GitError("--dry-run cannot be combined with approval options")
    with repository_lock(repository):
        verify_repository(repository)
        if recover_transaction(repository):
            print("Recovered an interrupted synchronization.")
        if args.command != "status" and not dry_run:
            instances = selected_instances(
                report, config, args.instance if args.command == "sync" else None
            )
            resolve_new_decisions(instances, config, args)
        if args.command != "status" and not dry_run:
            if dirty(repository):
                raise GitError("Repository has uncommitted changes; sync aborted")
            if config.git_pull and has_origin(repository):
                with activity("Downloading repository changes"):
                    pull(repository)
        with activity("Checking build files"):
            plans = make_plans(
                report,
                config,
                args.instance if args.command == "sync" else None,
                approved_only=args.command != "status" and not dry_run,
            )
        show_plans(plans, verbose=args.verbose)
        if args.command == "status" or dry_run or not any(p.changed for p in plans):
            return 0
        if config.git_commit:
            ensure_commit_identity(repository)
        # Persist stable destinations before the repository changes.
        save(config)
        changed_paths = apply_plans(plans, repository, TransferProgress())
        for plan in plans:
            if plan.changed:
                print(f"✓ {plan.instance.name}")
        print("Synchronization complete.")
        if config.git_commit and changed_paths:
            if stage(repository, changed_paths):
                commit(repository, f"mc_manager: sync {sum(p.changed for p in plans)} instance(s)")
                print("Git commit created.")
                if config.git_push and has_origin(repository):
                    push(repository)
                    print("Pushed to origin.")
        return 0


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
        print(f"Minecraft Manager {__version__}\nScanning local installations...", flush=True)
        with activity("Scanning local installations"):
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
                print("\nGit repository not configured.")
                print("Run mc_manager config --setup to enable sync.")
                return 0
            else:
                raise GitError("Repository is not configured. Run mc_manager config --setup.")
        return synchronize(report, config, args)
    except (KeyboardInterrupt, EOFError):
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (GitError, SafetyError, OSError, ValueError) as exc:
        sys.stdout.flush()
        print(f"mc_manager: {exc}", file=sys.stderr)
        return 1
