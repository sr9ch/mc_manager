import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mc_manager.discovery import (
    DirectoryAdapter,
    discover_report,
    parse_mmc_pack,
    parse_version_id,
    parse_version_metadata,
)
from mc_manager.generic import GenericFilesystemDiscovery, fingerprint
from mc_manager.shared_launchers import SharedLauncherAdapter, version_from_game


def put(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def version(root: Path, name: str, game: str = "1.21.1", library: str | None = None):
    data = {"id": name, "inheritsFrom": game}
    if library:
        data["libraries"] = [{"name": library}]
    put(root / "versions" / name / f"{name}.json", data)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = patch.dict(
            "os.environ",
            {
                "XDG_DATA_HOME": str(self.home / ".local/share"),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "XDG_STATE_HOME": str(self.home / ".local/state"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_prism_components_and_custom_game(self):
        root = self.home / ".local/share/PrismLauncher/instances/BetterMC"
        put(
            root / "mmc-pack.json",
            {
                "components": [
                    {"uid": "net.minecraft", "version": "1.20.1"},
                    {"uid": "net.minecraftforge", "version": "47.3.0"},
                ]
            },
        )
        (root / "instance.cfg").write_text("name=Better MC\n")
        (root / "minecraft/mods").mkdir(parents=True)
        found = DirectoryAdapter("prism", self.home).scan()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].name, "Better MC")
        self.assertEqual(found[0].minecraft.value, "1.20.1")
        self.assertEqual(
            (found[0].loader.value, found[0].loader_version.value), ("forge", "47.3.0")
        )
        self.assertEqual(found[0].game_dir, root / "minecraft")

    def test_mmc_loaders(self):
        for uid, expected in (
            ("net.fabricmc.fabric-loader", "fabric"),
            ("org.quiltmc.quilt-loader", "quilt"),
            ("net.neoforged", "neoforge"),
        ):
            with self.subTest(uid=uid):
                file = self.home / f"{expected}.json"
                put(
                    file,
                    {
                        "components": [
                            {"uid": "net.minecraft", "version": "1.21.1"},
                            {"uid": uid, "version": "1.0"},
                        ]
                    },
                )
                self.assertEqual(parse_mmc_pack(file)[1].value, expected)

    def test_shared_launchers_custom_path_and_dedup(self):
        game = self.home / "games/custom"
        version(game, "fabric-loader-0.16.10-1.21.1", library="net.fabricmc:fabric-loader:0.16.10")
        for path in (
            self.home / ".launcher/legacy.properties",
            self.home / ".tlauncher/tlauncher-2.0.properties",
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"minecraft.gamedir={game}\nselectedVersion=fabric-loader-0.16.10-1.21.1\n"
            )
        report = discover_report(self.home, {"generic": [game]})
        self.assertEqual(len(report.instances), 1)
        self.assertEqual(set(report.instances[0].launchers), {"legacy", "tlauncher"})
        self.assertEqual(report.instances[0].loader_version.value, "0.16.10")
        self.assertGreaterEqual(report.duplicates_merged, 1)

    def test_sklauncher_profiles_custom_directory(self):
        root = self.home / ".minecraft"
        game = self.home / "My Modpack"
        version(root, "forge-1.21.1-52.0.0", library="net.minecraftforge:forge:1.21.1-52.0.0")
        (game / "mods").mkdir(parents=True)
        put(
            root / "sklauncher/installations.json",
            {
                "workDir": str(root),
                "installations": [
                    {"name": "My Modpack", "gameDir": str(game), "versionId": "forge-1.21.1-52.0.0"}
                ],
            },
        )
        found = SharedLauncherAdapter("sklauncher", self.home).scan()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].game_dir, game)
        self.assertEqual(found[0].loader.value, "forge")

    def test_sklauncher_4_instances_file(self):
        root = self.home / ".sklauncher"
        game = root / "instances/pack"
        (game / "mods").mkdir(parents=True)
        version(root, "fabric-loader-0.16.10-1.21.1", library="net.fabricmc:fabric-loader:0.16.10")
        put(
            root / "instances.json",
            {
                "instances": [
                    {
                        "name": "Pack",
                        "directory": str(game),
                        "minecraftVersion": "1.21.1",
                        "versionId": "fabric-loader-0.16.10-1.21.1",
                        "gameType": "fabric",
                    },
                    {
                        "name": "Unplayed",
                        "directory": str(root / "instances/unplayed"),
                        "minecraftVersion": "latest-release",
                        "versionId": "latest-release",
                    },
                ]
            },
        )
        found = SharedLauncherAdapter("sklauncher", self.home).scan()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].name, "Pack")
        self.assertEqual(found[0].loader_version.value, "0.16.10")

    def test_inferred_loader_version_strips_game_version(self):
        self.assertEqual(parse_version_id("fabric-loader-0.16.10-1.21.1")[2].value, "0.16.10")

    def test_version_metadata_loaders_and_vanilla(self):
        cases = [
            ("net.fabricmc:fabric-loader:0.16.10", "fabric", "0.16.10"),
            ("net.minecraftforge:forge:1.21.1-52.0.0", "forge", "52.0.0"),
            ("net.neoforged:neoforge:21.1.1", "neoforge", "21.1.1"),
            ("org.quiltmc:quilt-loader:0.27.0", "quilt", "0.27.0"),
            (None, "vanilla", None),
        ]
        for i, (lib, expected, lv) in enumerate(cases):
            with self.subTest(expected=expected):
                root = self.home / f"game{i}"
                version(root, "1.21.1", library=lib)
                mc, loader, loader_version = version_from_game(root)
                self.assertEqual(
                    (mc.value, loader.value, loader_version.value), ("1.21.1", expected, lv)
                )

    def test_multiple_versions_shared_root_stays_unknown(self):
        root = self.home / "game"
        version(root, "one")
        version(root, "two")
        self.assertIsNone(version_from_game(root)[0].value)

    def test_broken_metadata(self):
        root = self.home / "bad"
        file = root / "versions/bad/bad.json"
        file.parent.mkdir(parents=True)
        file.write_text("{")
        self.assertIsNone(parse_version_metadata(file)[0].value)

    def test_launcher_installed_without_game(self):
        config = self.home / ".launcher/legacy.properties"
        config.parent.mkdir(parents=True)
        config.write_text("minecraft.gamedir=/missing\n")
        adapter = SharedLauncherAdapter("legacy", self.home)
        self.assertTrue(adapter.installed())
        self.assertEqual(adapter.scan(), [])

    def test_generic_fingerprint_and_depth(self):
        base = self.home / ".local/share"
        game = base / "customlauncher/profiles/MyPack/game"
        for name in ("mods", "config", "resourcepacks", "shaderpacks"):
            (game / name).mkdir(parents=True, exist_ok=True)
        self.assertTrue(fingerprint(game).accepted)
        scanner = GenericFilesystemDiscovery(self.home, max_depth=4)
        self.assertIn(game, [x.game_dir for x in scanner.scan().instances])
        scanner = GenericFilesystemDiscovery(self.home, max_depth=2)
        self.assertNotIn(game, [x.game_dir for x in scanner.scan().instances])

    def test_mods_only_rejected_and_partial_root(self):
        mods = self.home / ".local/share/irrelevant/mods"
        mods.mkdir(parents=True)
        self.assertFalse(fingerprint(mods.parent).accepted)
        partial = self.home / ".local/share/partial/versions"
        partial.mkdir(parents=True)
        self.assertFalse(fingerprint(partial.parent).accepted)

    def test_generic_without_mods_and_unknown_launcher(self):
        root = self.home / ".local/share/custom/game"
        version(root, "1.21.1")
        (root / "libraries").mkdir()
        report = discover_report(self.home)
        self.assertEqual(len(report.instances), 1)
        self.assertEqual(report.instances[0].client, "generic")
        self.assertEqual(report.instances[0].minecraft.value, "1.21.1")

    def test_symlink_duplicate_and_loop(self):
        root = self.home / ".local/share/custom/game"
        (root / "versions").mkdir(parents=True)
        (root / "libraries").mkdir()
        (root / "loop").symlink_to(root, target_is_directory=True)
        (self.home / ".local/share/alias").symlink_to(root, target_is_directory=True)
        result = GenericFilesystemDiscovery(self.home).scan()
        self.assertEqual(len(result.instances), 1)

    def test_generic_prism_duplicate(self):
        root = self.home / ".local/share/PrismLauncher/instances/Pack"
        put(root / "mmc-pack.json", {"components": [{"uid": "net.minecraft", "version": "1.21.1"}]})
        (root / "minecraft/mods").mkdir(parents=True)
        (root / "minecraft/config").mkdir()
        report = discover_report(self.home)
        self.assertEqual(len(report.instances), 1)
        self.assertEqual(report.instances[0].client, "prism")
        self.assertIn("generic", report.instances[0].discovery_sources)

    def test_excluded_directory(self):
        game = self.home / ".local/share/node_modules/game"
        (game / "versions").mkdir(parents=True)
        (game / "libraries").mkdir()
        self.assertEqual(GenericFilesystemDiscovery(self.home).scan().instances, [])


if __name__ == "__main__":
    unittest.main()
