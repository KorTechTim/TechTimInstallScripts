import json
import tempfile
import unittest
from pathlib import Path

from panels.palworld.app import main


class CommunityServerSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_data_dir = main.DATA_DIR
        self.original_launch_settings_file = main.SERVER_LAUNCH_SETTINGS_FILE
        self.data_dir = Path(self.temporary_directory.name)
        main.DATA_DIR = self.data_dir
        main.SERVER_LAUNCH_SETTINGS_FILE = self.data_dir / "server-launch-settings.json"

    def tearDown(self):
        main.DATA_DIR = self.original_data_dir
        main.SERVER_LAUNCH_SETTINGS_FILE = self.original_launch_settings_file
        self.temporary_directory.cleanup()

    def test_missing_or_invalid_file_defaults_to_dedicated_server(self):
        self.assertEqual(
            main.read_server_launch_settings(),
            {"CommunityServer": False},
        )

        main.SERVER_LAUNCH_SETTINGS_FILE.write_text("{invalid", encoding="utf-8")
        self.assertFalse(main.read_server_launch_settings()["CommunityServer"])

        main.SERVER_LAUNCH_SETTINGS_FILE.write_text(
            json.dumps({"CommunityServer": ["wrong-type"]}),
            encoding="utf-8",
        )
        self.assertFalse(main.read_server_launch_settings()["CommunityServer"])

    def test_launch_setting_is_written_atomically_with_only_supported_key(self):
        path = main.write_server_launch_settings({
            "CommunityServer": True,
            "Ignored": "value",
        })

        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")),
            {"CommunityServer": True},
        )
        self.assertEqual(list(self.data_dir.glob("*.tmp")), [])

    def test_server_command_adds_public_lobby_only_when_enabled(self):
        dedicated = main.build_server_command({
            "PublicPort": 8211,
            "CommunityServer": False,
        })
        community = main.build_server_command({
            "PublicPort": 8211,
            "CommunityServer": True,
        })

        self.assertNotIn("-publiclobby", dedicated)
        self.assertEqual(community.count("-publiclobby"), 1)
        self.assertIn("-port=8211", dedicated)
        self.assertIn("-port=8211", community)

    def test_community_setting_is_not_written_to_palworld_ini(self):
        config_path = main.write_config({
            **main.default_config(),
            "CommunityServer": True,
        })
        contents = config_path.read_text(encoding="utf-8")

        self.assertNotIn("CommunityServer", contents)
        self.assertNotIn("PublicLobby", contents)

    def test_config_api_saves_and_returns_community_setting(self):
        original_running_check = main.is_server_container_running
        original_require_auth = main.require_auth
        main.is_server_container_running = lambda: False
        main.require_auth = lambda request: "admin"

        try:
            response = main.save_config(
                main.ConfigRequest(CommunityServer=True),
                request=None,
            )
        finally:
            main.is_server_container_running = original_running_check
            main.require_auth = original_require_auth

        self.assertTrue(response["config"]["CommunityServer"])
        self.assertTrue(main.read_server_launch_settings()["CommunityServer"])

    def test_config_api_keeps_running_server_lock(self):
        original_running_check = main.is_server_container_running
        original_require_auth = main.require_auth
        main.is_server_container_running = lambda: True
        main.require_auth = lambda request: "admin"

        try:
            with self.assertRaises(main.HTTPException) as context:
                main.save_config(main.ConfigRequest(CommunityServer=True), request=None)
        finally:
            main.is_server_container_running = original_running_check
            main.require_auth = original_require_auth

        self.assertEqual(context.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
