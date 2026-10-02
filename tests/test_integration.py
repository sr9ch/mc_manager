import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class EndToEndTests(unittest.TestCase):
    def test_real_discovery_sync_and_git_commit(self):
        with tempfile.TemporaryDirectory(prefix="mc-manager-e2e-") as temporary:
            home = Path(temporary)
            data = home / "data"
            config_home = home / "config"
            state = home / "state"
            instance = data / "PrismLauncher/instances/Pack"
            game = instance / "minecraft"
            (game / "mods").mkdir(parents=True)
            (instance / "instance.cfg").write_text("name=Pack\n")
            (instance / "mmc-pack.json").write_text(
                json.dumps({"components": [{"uid": "net.minecraft", "version": "1.21.1"}]})
            )
            mod = game / "mods/example.jar"
            mod.write_text("first")

            repository = data / "mc_manager/repository"
            repository.mkdir(parents=True)
            for command in (
                ["git", "init", "-q", str(repository)],
                ["git", "-C", str(repository), "config", "user.name", "Test"],
                ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
                ["git", "-C", str(repository), "commit", "--allow-empty", "-q", "-m", "init"],
            ):
                subprocess.run(command, check=True)
            config = config_home / "mc_manager/config.toml"
            config.parent.mkdir(parents=True)
            config.write_text(
                f"[repository]\npath = {json.dumps(str(repository))}\n"
                "[sync]\nask_before_sync = false\ngit_commit = true\ngit_pull = false\n"
            )
            environment = {
                **os.environ,
                "HOME": str(home),
                "USERPROFILE": str(home),
                "APPDATA": str(home / "AppData/Roaming"),
                "LOCALAPPDATA": str(home / "AppData/Local"),
                "XDG_DATA_HOME": str(data),
                "XDG_CONFIG_HOME": str(config_home),
                "XDG_STATE_HOME": str(state),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }

            def invoke(*arguments: str) -> str:
                result = subprocess.run(
                    [sys.executable, "-m", "mc_manager", *arguments],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                return result.stdout

            target = repository / "minecraft/prism/pack/mods/example.jar"
            first_scan = invoke("scan")
            self.assertIn("1 unique installation", first_scan)
            self.assertIn("First scan saved as baseline", first_scan)
            self.assertIn("+ mods/example.jar", invoke("sync", "--dry-run"))
            self.assertFalse(target.exists())
            invoke("sync")
            self.assertEqual(target.read_text(), "first")
            mod.write_text("second")
            self.assertIn("Content changed in Pack: +0 ~1 -0 files", invoke("scan"))
            self.assertIn("~ mods/example.jar", invoke("sync"))
            self.assertEqual(target.read_text(), "second")
            mod.unlink()
            self.assertIn("- mods/example.jar", invoke("sync"))
            self.assertFalse(target.exists())
            (instance / "mmc-pack.json").write_text(
                json.dumps({"components": [{"uid": "net.minecraft", "version": "1.21.2"}]})
            )
            self.assertIn("Minecraft version changed in Pack: 1.21.1 → 1.21.2", invoke("scan"))
            self.assertEqual(
                subprocess.check_output(
                    ["git", "-C", str(repository), "status", "--porcelain"], text=True
                ),
                "",
            )
            self.assertIn("1 changed; 0 unchanged", invoke("status"))
            self.assertIn("1 unique installation", invoke("scan"))


if __name__ == "__main__":
    unittest.main()
