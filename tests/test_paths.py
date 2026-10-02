import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mc_manager.discovery import candidate_roots
from mc_manager.generic import search_roots
from mc_manager.paths import config_dir, data_dir, minecraft_dir, state_dir


class PlatformPathTests(unittest.TestCase):
    def test_windows_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            roaming = home / "AppData/Roaming"
            local = home / "AppData/Local"
            with patch("mc_manager.paths.sys.platform", "win32"):
                with patch("mc_manager.discovery.sys.platform", "win32"):
                    with patch("mc_manager.generic.sys.platform", "win32"):
                        with patch.dict(
                            "os.environ",
                            {"APPDATA": str(roaming), "LOCALAPPDATA": str(local)},
                            clear=True,
                        ):
                            self.assertEqual(config_dir(home), roaming / "mc_manager")
                            self.assertEqual(data_dir(home), local / "mc_manager")
                            self.assertEqual(state_dir(home), local / "mc_manager")
                            self.assertEqual(minecraft_dir(home), roaming / ".minecraft")
                            self.assertIn(
                                roaming / "PrismLauncher/instances", candidate_roots(home, "prism")
                            )
                            self.assertIn(roaming, search_roots(home))

    def test_macos_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            support = home / "Library/Application Support"
            with patch("mc_manager.paths.sys.platform", "darwin"):
                with patch("mc_manager.discovery.sys.platform", "darwin"):
                    with patch("mc_manager.generic.sys.platform", "darwin"):
                        with patch.dict("os.environ", {}, clear=True):
                            self.assertEqual(config_dir(home), support / "mc_manager")
                            self.assertEqual(data_dir(home), support / "mc_manager")
                            self.assertEqual(state_dir(home), support / "mc_manager")
                            self.assertEqual(minecraft_dir(home), support / "minecraft")
                            self.assertIn(
                                support / "PrismLauncher/instances", candidate_roots(home, "prism")
                            )
                            self.assertNotIn(home / ".var/app", search_roots(home))


if __name__ == "__main__":
    unittest.main()
