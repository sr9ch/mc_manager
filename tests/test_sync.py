import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mc_manager.config import Config, load, save
from mc_manager.git import GitError, dirty, stage, validate_remote_url, verify_repository
from mc_manager.models import DetectedValue, MinecraftInstance
from mc_manager.safety import SafetyError, safe_relative, slug, world_slug
from mc_manager.sync import apply_plan, apply_plans, destination_for, plan_instance, source_files


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.game = self.base / "game"
        self.game.mkdir()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.instance = MinecraftInstance(
            "prism",
            "Better MC",
            self.game,
            self.game,
            DetectedValue("1.21.1", "detected"),
            DetectedValue("fabric", "detected"),
        )
        self.relative = Path("minecraft/prism/better-mc")

    def write(self, relative: str, data: str = "content"):
        path = self.game / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        return path

    def plan(self):
        return plan_instance(self.instance, self.repo, self.relative)

    def link(self, path: Path, target: Path, directory: bool = False) -> None:
        try:
            path.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            self.skipTest(f"Symlinks are unavailable on this runner: {exc}")

    def test_add_modify_delete_and_manifest(self):
        source = self.write("mods/sodium.jar", "a")
        first = self.plan()
        self.assertEqual(first.changes[0].kind, "add")
        apply_plan(first, self.repo)
        manifest = json.loads((self.repo / self.relative / "mc_manager.json").read_text())
        self.assertNotIn(str(self.game), json.dumps(manifest))
        self.assertEqual(self.plan().changes, [])
        source.write_text("b")
        second = self.plan()
        self.assertEqual(second.changes[0].kind, "modify")
        apply_plan(second, self.repo)
        source.unlink()
        third = self.plan()
        self.assertEqual(third.changes[0].kind, "delete")
        apply_plan(third, self.repo)
        self.assertFalse((self.repo / self.relative / "mods/sodium.jar").exists())

    def test_transaction_reports_loading_and_writing_progress(self):
        self.write("mods/a.jar", "a" * 10)
        updates = []
        apply_plans(
            [self.plan()],
            self.repo,
            lambda phase, current, total: updates.append((phase, current, total)),
        )
        self.assertIn(("Loading files", 10, 10), updates)
        self.assertEqual(updates[-1][0], "Writing repository")
        self.assertEqual(updates[-1][1], updates[-1][2])

    def test_ignore_and_datapacks(self):
        self.write("mods/keep.jar")
        self.write("mods/ignore.jar")
        self.write("logs/latest.log")
        self.write("saves/My World/datapacks/a/pack.mcmeta")
        self.write("saves/My World/level.dat")
        self.write(".mcmanagerignore", "mods/ignore.jar\n")
        files = source_files(self.instance)
        self.assertIn("mods/keep.jar", files)
        self.assertNotIn("mods/ignore.jar", files)
        self.assertFalse(any("level.dat" in x or "logs" in x for x in files))
        self.assertTrue(any(x.startswith("datapacks/my-world-") for x in files))

    def test_secret_filter(self):
        self.write("config/auth-token.json", "abc")
        self.write("config/settings.json", '{"access_token":"abcdefghi"}')
        self.write("config/camel.json", '{"clientSecret":"abcdefghi"}')
        self.write("config/private.zip", "opaque")
        self.write("config/public.json", '{"display":"abc"}')
        files = source_files(self.instance)
        self.assertEqual(list(files), ["config/public.json"])

    def test_source_symlink_is_rejected(self):
        outside = self.base / "secret.txt"
        outside.write_text("secret")
        (self.game / "mods").mkdir()
        self.link(self.game / "mods/link.jar", outside)
        with self.assertRaisesRegex(SafetyError, "Symlinked source file"):
            source_files(self.instance)

    def test_ignored_symlinked_category_is_skipped(self):
        shared = self.base / "shared"
        shared.mkdir()
        self.link(self.game / "mods", shared, directory=True)
        self.assertEqual(source_files(self.instance, ["mods"]), {})

    def test_replaced_source_directory_does_not_delete_backup(self):
        mod = self.write("mods/nested/a.jar")
        apply_plan(self.plan(), self.repo)
        backup = self.repo / self.relative / "mods/nested/a.jar"
        shared = self.base / "shared"
        mod.parent.rename(shared)
        self.link(mod.parent, shared, directory=True)
        with self.assertRaisesRegex(SafetyError, "Symlinked source directory"):
            self.plan()
        self.assertTrue(backup.exists())

    def test_destination_symlink_is_rejected(self):
        self.write("mods/a.jar")
        self.link(self.repo / "minecraft", self.base, directory=True)
        with self.assertRaises(SafetyError):
            self.plan()

    def test_unmanaged_file_preserved(self):
        self.write("mods/a.jar")
        target = self.repo / self.relative / "mods/a.jar"
        target.parent.mkdir(parents=True)
        target.write_text("content")
        with self.assertRaises(SafetyError):
            self.plan()
        self.assertEqual(target.read_text(), "content")

    def test_repo_edit_blocks_overwrite(self):
        source = self.write("config/a.json", "one")
        apply_plan(self.plan(), self.repo)
        target = self.repo / self.relative / "config/a.json"
        target.write_text("user edit")
        source.write_text("new local")
        with self.assertRaises(SafetyError):
            self.plan()

    def test_malformed_manifest_is_reported_as_safety_error(self):
        manifest = self.repo / self.relative / "mc_manager.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("[]")
        with self.assertRaisesRegex(SafetyError, "Unknown manifest schema"):
            self.plan()
        manifest.write_text(json.dumps({"schema": 1, "files": {"mods/a.jar": "z" * 64}}))
        with self.assertRaisesRegex(SafetyError, "Invalid hash"):
            self.plan()
        manifest.write_text(json.dumps({"schema": 1, "files": {"other/a.jar": "a" * 64}}))
        with self.assertRaisesRegex(SafetyError, "Unmanaged path"):
            self.plan()

    def test_unreadable_source_directory_does_not_plan_deletions(self):
        self.write("mods/a.jar")

        def fail_walk(_source_dir, **options):
            options["onerror"](PermissionError("source directory denied"))

        with patch("mc_manager.sync.os.walk", side_effect=fail_walk):
            with self.assertRaisesRegex(SafetyError, "source directory denied"):
                self.plan()
        self.assertFalse((self.repo / self.relative).exists())

    def test_changed_later_source_does_not_partially_apply_plan(self):
        self.write("mods/a.jar", "first")
        later = self.write("mods/z.jar", "second")
        plan = self.plan()
        later.write_text("changed after planning")
        with self.assertRaises(SafetyError):
            apply_plan(plan, self.repo)
        self.assertFalse((self.repo / self.relative / "mods/a.jar").exists())
        self.assertFalse((self.repo / self.relative / "mc_manager.json").exists())

    def test_unmanaged_file_added_after_plan_is_preserved(self):
        self.write("mods/a.jar", "same bytes")
        plan = self.plan()
        target = self.repo / self.relative / "mods/a.jar"
        target.parent.mkdir(parents=True)
        target.write_text("same bytes")
        with self.assertRaises(SafetyError):
            apply_plan(plan, self.repo)
        self.assertEqual(target.read_text(), "same bytes")

    def test_failure_during_apply_restores_all_instances(self):
        source = self.write("mods/a.jar", "initial")
        apply_plan(self.plan(), self.repo)
        other_game = self.base / "other-game"
        (other_game / "mods").mkdir(parents=True)
        (other_game / "mods/b.jar").write_text("other")
        other = MinecraftInstance("prism", "Other", other_game, other_game)
        source.write_text("changed")
        first = self.plan()
        second = plan_instance(other, self.repo, Path("minecraft/prism/other"))
        original_replace = __import__("os").replace

        def fail_once(source_path, destination_path):
            if str(destination_path).endswith("other/mods/b.jar"):
                with patch("mc_manager.transaction.os.replace", original_replace):
                    raise OSError("simulated disk failure")
            return original_replace(source_path, destination_path)

        with patch("mc_manager.transaction.os.replace", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "simulated disk failure"):
                apply_plans([first, second], self.repo)
        self.assertEqual((self.repo / self.relative / "mods/a.jar").read_text(), "initial")
        self.assertFalse((self.repo / "minecraft/prism/other/mods/b.jar").exists())
        self.assertFalse((self.repo / ".mc_manager-transaction").exists())

    def test_recovery_restores_interrupted_write(self):
        from mc_manager.transaction import (
            TRANSACTION,
            _prepare,
            _write_journal,
            recover_transaction,
        )

        source = self.write("mods/a.jar", "initial")
        apply_plan(self.plan(), self.repo)
        source.write_text("changed")
        plan = self.plan()
        root = self.repo / TRANSACTION
        root.mkdir()
        (root / "new").mkdir()
        (root / "old").mkdir()
        operations = _prepare([plan], self.repo, root)
        _write_journal(root, "ready", operations)
        target = self.repo / self.relative / "mods/a.jar"
        __import__("os").replace(target, root / "old/0")
        __import__("os").replace(root / "new/0", target)
        self.assertTrue(recover_transaction(self.repo))
        self.assertEqual(target.read_text(), "initial")
        self.assertFalse(root.exists())

    def test_recovery_cleans_incomplete_initial_journal(self):
        from mc_manager.transaction import TRANSACTION, recover_transaction

        root = self.repo / TRANSACTION
        root.mkdir()
        (root / ".journal-incomplete").write_text("partial")
        self.assertTrue(recover_transaction(self.repo))
        self.assertFalse(root.exists())

    def test_destination_collision_and_slug(self):
        config = Config()
        used = set()
        first = destination_for(self.instance, config, used)
        other_dir = self.base / "other"
        other_dir.mkdir()
        other = MinecraftInstance("prism", "Better MC", other_dir, other_dir)
        second = destination_for(other, config, used)
        self.assertNotEqual(first, second)
        self.assertEqual(slug("  Bètter MC!  "), "bètter-mc")
        self.assertEqual(slug("CON"), "con-instance")
        self.assertTrue(world_slug("Новый мир").startswith("новый-мир-"))
        with self.assertRaises(SafetyError):
            safe_relative("../escape")

    def test_config_roundtrip(self):
        path = self.base / "config.toml"
        config = Config(self.repo, excluded_clients=["legacy"], ignores=["mods/private*"])
        config.destinations[self.instance.key] = self.relative.as_posix()
        config.decisions['["game","1.21.1","fabric",null]'] = False
        save(config, path)
        parsed = load(path)
        self.assertEqual(parsed.repository, self.repo)
        self.assertEqual(parsed.destinations, config.destinations)
        self.assertEqual(parsed.decisions, config.decisions)
        self.assertEqual(parsed.ignores, ["mods/private*"])

    def test_git_status_and_stage(self):
        verify_repository(self.repo)
        self.assertFalse(dirty(self.repo))
        path = self.repo / "file.txt"
        path.write_text("x")
        self.assertTrue(dirty(self.repo))
        self.assertTrue(stage(self.repo, [path]))

    def test_remote_url_validation(self):
        for url in (
            "git@github.com:someone/minecraft-backup.git",
            "ssh://git@example.org/someone/minecraft-backup.git",
            "https://github.com/someone/minecraft-backup.git",
        ):
            validate_remote_url(url)
        for url in (
            "file:///tmp/repo.git",
            "http://example.org/repo.git",
            "https://user:token@example.org/repo.git",
            "https://example.org/repo.git?token=secret",
        ):
            with self.assertRaises(GitError):
                validate_remote_url(url)


if __name__ == "__main__":
    unittest.main()
