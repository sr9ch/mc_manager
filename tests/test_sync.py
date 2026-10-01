import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from mc_manager.config import Config, load, save
from mc_manager.git import dirty, stage, verify_repository
from mc_manager.models import DetectedValue, MinecraftInstance
from mc_manager.safety import SafetyError, safe_relative, slug
from mc_manager.sync import apply_plan, destination_for, plan_instance, source_files


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

    def test_source_symlink_is_skipped(self):
        outside = self.base / "secret.txt"
        outside.write_text("secret")
        (self.game / "mods").mkdir()
        (self.game / "mods/link.jar").symlink_to(outside)
        self.assertEqual(source_files(self.instance), {})

    def test_destination_symlink_is_rejected(self):
        self.write("mods/a.jar")
        (self.repo / "minecraft").symlink_to(self.base, target_is_directory=True)
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

    def test_destination_collision_and_slug(self):
        config = Config()
        used = set()
        first = destination_for(self.instance, config, used)
        other_dir = self.base / "other"
        other_dir.mkdir()
        other = MinecraftInstance("prism", "Better MC", other_dir, other_dir)
        second = destination_for(other, config, used)
        self.assertNotEqual(first, second)
        self.assertEqual(slug("  Bètter MC!  "), "b-tter-mc")
        with self.assertRaises(SafetyError):
            safe_relative("../escape")

    def test_config_roundtrip(self):
        path = self.base / "config.toml"
        config = Config(self.repo, excluded_clients=["legacy"], ignores=["mods/private*"])
        config.destinations[self.instance.key] = self.relative.as_posix()
        save(config, path)
        parsed = load(path)
        self.assertEqual(parsed.repository, self.repo)
        self.assertEqual(parsed.destinations, config.destinations)
        self.assertEqual(parsed.ignores, ["mods/private*"])

    def test_git_status_and_stage(self):
        verify_repository(self.repo)
        self.assertFalse(dirty(self.repo))
        path = self.repo / "file.txt"
        path.write_text("x")
        self.assertTrue(dirty(self.repo))
        self.assertTrue(stage(self.repo, [path]))


if __name__ == "__main__":
    unittest.main()
