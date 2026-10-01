import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mc_manager.cli import main
from mc_manager.config import Config, save
from mc_manager.discovery import DiscoveryReport
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

    def test_dirty_repo_blocks_sync(self):
        (self.repo / "user.txt").write_text("leave me alone")
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["sync"]), 1)
        self.assertIn("uncommitted changes", err.getvalue())
        self.assertFalse((self.repo / "minecraft").exists())


if __name__ == "__main__":
    unittest.main()
