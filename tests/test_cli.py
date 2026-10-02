import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mc_manager.cli import main, setup
from mc_manager.config import Config, load, save
from mc_manager.discovery import DiscoveryReport
from mc_manager.git import GitError, repository_lock
from mc_manager.models import DetectedValue, MinecraftInstance


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        self.game = self.root / "game"
        (self.game / "mods").mkdir(parents=True)
        (self.game / "mods/a.jar").write_text("mod")
        instance = MinecraftInstance(
            "prism",
            "Test Pack",
            self.game,
            self.game,
            DetectedValue("1.21.1", "detected"),
            DetectedValue("fabric", "detected"),
        )
        self.report = DiscoveryReport([instance], {"prism": 1, "generic": 0}, {"prism"}, [])
        self.discovery = patch("mc_manager.cli.discover_report", return_value=self.report)
        self.discovery.start()
        self.addCleanup(self.discovery.stop)
        save(Config(self.repo))

    def test_dry_run_then_sync_then_status(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["sync", "Test Pack", "--dry-run"]), 0)
        self.assertFalse((self.repo / "minecraft").exists())
        self.assertIn("+ mods/a.jar", output.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["sync", "Test Pack", "--all-new"]), 0)
        self.assertTrue((self.repo / "minecraft/prism/test-pack/mods/a.jar").exists())
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["status"]), 0)
        self.assertIn("0 changed", output.getvalue())

    def test_new_build_can_be_declined_once(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["sync", "--skip-new"]), 0)
        self.assertFalse((self.repo / "minecraft").exists())
        self.assertEqual(list(load().decisions.values()), [False])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["sync", "--all-new"]), 0)
        self.assertFalse((self.repo / "minecraft").exists())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["sync", "Test Pack", "--reconsider", "--all-new"]), 0)
        self.assertTrue((self.repo / "minecraft/prism/test-pack/mods/a.jar").exists())
        self.assertEqual(list(load().decisions.values()), [True])

    def test_individual_decisions_are_saved_and_not_asked_again(self):
        other_game = self.root / "other-game"
        (other_game / "mods").mkdir(parents=True)
        (other_game / "mods/b.jar").write_text("other")
        self.report.instances.append(
            MinecraftInstance(
                "prism", "Other Pack", other_game, other_game, DetectedValue("1.20.1")
            )
        )
        with patch("mc_manager.cli.sys.stdin") as stdin:
            stdin.isatty.return_value = True
            with patch("builtins.input", side_effect=["e", "y", "n"]) as prompt:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main([]), 0)
        self.assertEqual(prompt.call_count, 3)
        self.assertEqual(sorted(load().decisions.values()), [False, True])
        self.assertTrue((self.repo / "minecraft/prism/test-pack/mods/a.jar").exists())
        self.assertFalse((self.repo / "minecraft/prism/other-pack/mods/b.jar").exists())
        with patch("mc_manager.cli.sys.stdin") as stdin:
            stdin.isatty.return_value = True
            with patch("builtins.input", side_effect=AssertionError("asked again")):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main([]), 0)

    def test_changed_version_is_a_new_one_time_decision(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["sync", "--all-new"]), 0)
        old = self.report.instances[0]
        self.report.instances[0] = MinecraftInstance(
            old.client,
            old.name,
            old.root,
            old.game_dir,
            DetectedValue("1.21.2", "detected"),
            old.loader,
        )
        with patch("mc_manager.cli.sys.stdin") as stdin:
            stdin.isatty.return_value = True
            with patch("builtins.input", return_value="n") as prompt:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main([]), 0)
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(len(load().decisions), 2)

    def test_unknown_build_waits_for_interactive_approval(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["sync"]), 0)
        self.assertIn("await a decision", output.getvalue())
        self.assertEqual(load().decisions, {})
        self.assertFalse((self.repo / "minecraft").exists())

    def test_large_plan_is_short_by_default_and_complete_when_verbose(self):
        for number in range(20):
            (self.game / "mods" / f"extra-{number:02}.jar").write_text("mod")

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["sync", "--dry-run"]), 0)
        self.assertIn("+21 added", output.getvalue())
        self.assertIn("... 9 more paths (use --verbose for all)", output.getvalue())
        self.assertNotIn("+ mods/extra-19.jar", output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--verbose", "sync", "--dry-run"]), 0)
        self.assertIn("+ mods/extra-19.jar", output.getvalue())
        self.assertNotIn("more paths", output.getvalue())

    def test_dirty_repo_blocks_sync(self):
        (self.repo / "user.txt").write_text("leave me alone")
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["sync"]), 1)
        self.assertIn("uncommitted changes", err.getvalue())
        self.assertFalse((self.repo / "minecraft").exists())

    def test_sync_recovers_interrupted_cleanup_before_dirty_check(self):
        transaction = self.repo / ".mc_manager-transaction"
        transaction.mkdir()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["sync", "--all-new"]), 0)
        self.assertIn("Recovered an interrupted synchronization", output.getvalue())
        self.assertFalse(transaction.exists())

    def test_concurrent_repository_use_is_reported(self):
        with repository_lock(self.repo):
            with patch("sys.stderr", new_callable=io.StringIO) as err:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["sync"]), 1)
        self.assertIn("Another mc_manager process", err.getvalue())
        self.assertFalse((self.repo / "minecraft").exists())

    def test_setup_existing_local_repository(self):
        with patch("builtins.input", side_effect=[str(self.repo)]):
            with redirect_stdout(io.StringIO()):
                chosen = setup(self.report)
        self.assertEqual(chosen.repository.resolve(), self.repo.resolve())
        self.assertEqual(chosen.excluded_instances, [])
        self.assertEqual(load().excluded_clients, [])

    def test_repeating_setup_preserves_sync_settings_and_does_not_sync(self):
        existing = Config(
            self.repo,
            ask_before_sync=False,
            git_commit=False,
            git_pull=True,
            ignores=["mods/private-*"],
            destinations={self.report.instances[0].key: "minecraft/prism/old-name"},
        )
        save(existing)
        with patch("mc_manager.cli.sys.stdin") as stdin:
            stdin.isatty.return_value = True
            with patch("builtins.input", side_effect=[str(self.repo)]):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["config", "--setup"]), 0)
        updated = load()
        self.assertFalse(updated.ask_before_sync)
        self.assertFalse(updated.git_commit)
        self.assertTrue(updated.git_pull)
        self.assertEqual(updated.ignores, existing.ignores)
        self.assertEqual(updated.destinations, existing.destinations)
        self.assertFalse((self.repo / "minecraft").exists())

    def test_existing_remote_clone_can_be_selected_again(self):
        url = "git@github.com:example/minecraft-configs.git"
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", url], check=True)
        save(Config(self.repo, remote=url))
        with patch("builtins.input", side_effect=[url, ""]):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(setup(self.report).repository, self.repo)
        with patch("builtins.input", side_effect=[str(self.repo)]):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(setup(self.report).remote, url)
        save(Config(self.repo))
        with patch("builtins.input", side_effect=[url, str(self.repo)]):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(setup(self.report).remote, url)

    def test_setup_requires_repository_path(self):
        with patch("builtins.input", return_value=""):
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(GitError, "repository is required"):
                    setup(self.report)

    def test_invalid_config_reports_a_clear_error(self):
        path = self.root / "config/mc_manager/config.toml"
        path.write_text('[sync]\ngit_commit = "false"\n')
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(main(["config"]), 1)
        self.assertIn("sync.git_commit must be true or false", err.getvalue())

    def test_scan_output_groups_launchers_and_installations(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["scan"]), 0)
        self.assertIn("Scanning local installations", output.getvalue())
        self.assertIn("Launchers\n", output.getvalue())
        self.assertIn("Installations\n", output.getvalue())
        self.assertNotIn("not found", output.getvalue())

    def test_missing_git_identity_blocks_before_copy(self):
        save(Config(self.repo, ask_before_sync=False, git_commit=True))
        with patch(
            "mc_manager.cli.ensure_commit_identity", side_effect=GitError("identity missing")
        ):
            with patch("sys.stderr", new_callable=io.StringIO) as err:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["sync", "--all-new"]), 1)
        self.assertIn("identity missing", err.getvalue())
        self.assertFalse((self.repo / "minecraft").exists())

    def test_default_launch_scans_without_repository(self):
        save(Config())
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([]), 0)
        self.assertIn("First scan saved as baseline", output.getvalue())
        self.assertIn("mc_manager config --setup", output.getvalue())
        self.assertFalse((self.repo / "minecraft").exists())
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([]), 0)
        self.assertIn("No changes since last scan", output.getvalue())

    def test_first_interactive_launch_does_not_ask_setup_questions(self):
        save(Config())
        with patch("mc_manager.cli.sys.stdin") as stdin:
            stdin.isatty.return_value = True
            with patch("builtins.input", side_effect=AssertionError("unexpected question")):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main([]), 0)
        self.assertIsNone(load().repository)
        self.assertFalse((self.repo / "minecraft").exists())


if __name__ == "__main__":
    unittest.main()
