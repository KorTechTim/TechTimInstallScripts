import os
import tempfile
import unittest


TEST_RUNTIME = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = os.path.join(TEST_RUNTIME.name, "data")
os.environ["HOST_DATA_DIR"] = os.path.join(TEST_RUNTIME.name, "host-data")
os.environ["BACKUP_DIR"] = os.path.join(TEST_RUNTIME.name, "backups")

from app import main


class MinecraftVersionListTests(unittest.TestCase):
    def test_manifest_releases_include_through_1_12_1(self):
        manifest = {
            "versions": [
                {"id": "1.13", "type": "release"},
                {"id": "1.12.2", "type": "release"},
                {"id": "1.12.1", "type": "release"},
                {"id": "1.12", "type": "release"},
            ]
        }

        self.assertEqual(
            main.releases_since_1_12_1(manifest),
            ["1.13", "1.12.2", "1.12.1"],
        )

    def test_fallback_versions_stop_at_1_12_1(self):
        self.assertIn("1.12.2", main.FALLBACK_MINECRAFT_VERSIONS)
        self.assertEqual(main.FALLBACK_MINECRAFT_VERSIONS[-1], "1.12.1")
        self.assertNotIn("1.12", main.FALLBACK_MINECRAFT_VERSIONS)

    def test_java_8_selection_uses_supported_itzg_image_tag(self):
        self.assertIn("8", main.JAVA_VERSIONS)
        self.assertEqual(
            main.runtime_image_for_config({"JavaVersion": "8"}),
            "itzg/minecraft-server:java8",
        )

    def test_install_marker_only_locks_matching_java_runtime(self):
        main.INSTALL_MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(main.INSTALL_MARKER_FILE.unlink, missing_ok=True)
        main.INSTALL_MARKER_FILE.write_text(
            "distribution=itzg-docker\n"
            "runtime_image=itzg/minecraft-server:java25\n",
            encoding="utf-8",
        )

        self.assertTrue(main.installed({"JavaVersion": "25"}))
        self.assertFalse(main.installed({"JavaVersion": "8"}))


if __name__ == "__main__":
    unittest.main()
