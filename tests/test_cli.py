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
from mc_manager.git import GitError
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
        save(Config(self.repo, ask_before_sync=False, git_commit=False))

    def test_dry_run_then_sync_then_status(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["sync", "Test Pack", "--dry-run"]), 0)
        self.assertFalse((self.repo / "minecraft").exists())
        self.assertIn("+ mods/a.jar", output.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["sync", "Test Pack"]), 0)
        self.assertTrue((self.repo / "minecraft/prism/test-pack/mods/a.jar").exists())
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["status"]), 0)
        self.assertIn("0 changed", output.getvalue())

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

    def test_setup_existing_local_repository(self):
        with patch("builtins.input", side_effect=[str(self.repo), "1", "prism"]):
            with redirect_stdout(io.StringIO()):
                chosen = setup(self.report)
        self.assertEqual(chosen.repository, self.repo)
        self.assertEqual(chosen.excluded_instances, [self.report.instances[0].key])
        self.assertEqual(load().excluded_clients, ["prism"])

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
            with patch("builtins.input", side_effect=[str(self.repo), "", ""]):
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
        with patch("builtins.input", side_effect=[url, "", "", ""]):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(setup(self.report).repository, self.repo)
        with patch("builtins.input", side_effect=[str(self.repo), "", ""]):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(setup(self.report).remote, url)
        save(Config(self.repo))
        with patch("builtins.input", side_effect=[url, str(self.repo), "", ""]):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(setup(self.report).remote, url)

    def test_setup_rejects_invalid_exclusions(self):
        for answers, message in (
            ([str(self.repo), "99"], "Invalid instance number"),
            ([str(self.repo), "", "unknown"], "Unknown launcher ID"),
        ):
            with self.subTest(answers=answers):
                with patch("builtins.input", side_effect=answers):
                    with redirect_stdout(io.StringIO()):
                        with self.assertRaisesRegex(GitError, message):
                            setup(self.report)
                self.assertEqual(load().excluded_clients, [])

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
                    self.assertEqual(main(["sync"]), 1)
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

    def test_first_interactive_launch_offers_repository_setup(self):
        save(Config())
        with patch("mc_manager.cli.sys.stdin") as stdin:
            stdin.isatty.return_value = True
            with patch("builtins.input", side_effect=["y", str(self.repo), "", "", "n"]):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main([]), 0)
        self.assertEqual(load().repository, self.repo)
        self.assertFalse((self.repo / "minecraft").exists())


if __name__ == "__main__":
    unittest.main()
