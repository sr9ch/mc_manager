import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mc_manager.discovery import DiscoveryReport
from mc_manager.models import DetectedValue, MinecraftInstance
from mc_manager.scan_state import report_changes


class ScanStateTests(unittest.TestCase):
    def test_detects_new_launcher_version_and_content_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "game"
            mods = game / "mods"
            mods.mkdir(parents=True)
            mod = mods / "example.jar"
            mod.write_bytes(b"first")
            state = root / "state/seen.json"

            def report(version: str, launchers: set[str]) -> DiscoveryReport:
                instance = MinecraftInstance(
                    "prism",
                    "Pack",
                    game,
                    game,
                    DetectedValue(version, "detected"),
                    DetectedValue("fabric", "detected"),
                )
                return DiscoveryReport([instance], {name: 1 for name in launchers}, launchers, [])

            self.assertEqual(
                report_changes(report("1.21.1", {"prism"}), state),
                ["First scan saved as baseline."],
            )
            self.assertEqual(
                report_changes(report("1.21.1", {"prism"}), state),
                ["No changes since last scan."],
            )
            mod.write_bytes(b"second")
            new_mod = mods / "new.jar"
            new_mod.write_bytes(b"new")
            changes = report_changes(report("1.21.2", {"prism", "sklauncher"}), state)
            self.assertIn("New launcher detected: sklauncher", changes)
            self.assertIn("Minecraft version changed in Pack: 1.21.1 → 1.21.2", changes)
            self.assertIn("Content changed in Pack: +1 ~1 -0 files", changes)
            self.assertIn("  + mods/new.jar", changes)
            self.assertIn("  ~ mods/example.jar", changes)
            new_mod.unlink()
            changes = report_changes(report("1.21.2", {"prism"}), state)
            self.assertIn("Launcher no longer detected: sklauncher", changes)
            self.assertIn("  - mods/new.jar", changes)
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)

    def test_upgrades_previous_state_without_false_new_instances(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "game"
            game.mkdir()
            state = root / "seen.json"
            state.write_text(json.dumps({"clients": ["prism"], "instances": [str(game)]}))
            instance = MinecraftInstance("prism", "Pack", game, game)
            report = DiscoveryReport([instance], {"prism": 1}, {"prism"}, [])
            self.assertEqual(report_changes(report, state), ["No changes since last scan."])
            self.assertEqual(json.loads(state.read_text())["schema"], 3)

    def test_previous_instance_path_alias_does_not_look_new(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "game"
            game.mkdir()
            alias = root / "alias"
            try:
                alias.symlink_to(root, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Directory symlinks unavailable: {exc}")
            state = root / "seen.json"
            state.write_text(json.dumps({"clients": ["prism"], "instances": [str(alias / "game")]}))
            instance = MinecraftInstance("prism", "Pack", game, game)
            report = DiscoveryReport([instance], {"prism": 1}, {"prism"}, [])
            self.assertEqual(report_changes(report, state), ["No changes since last scan."])

    def test_corrupt_state_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "seen.json"
            state.write_text("{broken", encoding="utf-8")
            report = DiscoveryReport([], {}, set(), [])
            lines = report_changes(report, state)
            self.assertIn("scan history was left untouched", lines[0])
            self.assertEqual(state.read_text(), "{broken")

    def test_failed_content_scan_does_not_advance_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "game"
            game.mkdir()
            state = root / "seen.json"
            instance = MinecraftInstance("prism", "Pack", game, game)
            report = DiscoveryReport([instance], {"prism": 1}, {"prism"}, [])
            with patch("mc_manager.scan_state.source_files", side_effect=PermissionError("denied")):
                lines = report_changes(report, state)
            self.assertIn("First scan could not be saved", lines[0])
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
