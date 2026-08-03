import tempfile
import unittest
from pathlib import Path

from panels.palworld.app import main


class VoiceChatSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_data_dir = main.DATA_DIR
        main.DATA_DIR = Path(self.temporary_directory.name)

    def tearDown(self):
        main.DATA_DIR = self.original_data_dir
        self.temporary_directory.cleanup()

    def test_defaults_match_official_palworld_settings(self):
        self.assertEqual(
            main.read_voice_chat_settings(),
            {
                "enabled": False,
                "max_volume_distance": 3000.0,
                "zero_volume_distance": 15000.0,
            },
        )

    def test_save_updates_voice_chat_without_changing_other_options(self):
        main.write_config({
            **main.default_config(),
            "AdvancedOptions": {"ExpRate": 2.5},
        })
        original_running_check = main.is_server_container_running
        original_require_auth = main.require_auth
        main.is_server_container_running = lambda: False
        main.require_auth = lambda request: "admin"

        try:
            response = main.save_voice_chat(
                main.VoiceChatSettingsRequest(
                    enabled=True,
                    max_volume_distance=4200,
                    zero_volume_distance=18000,
                ),
                request=None,
            )
        finally:
            main.is_server_container_running = original_running_check
            main.require_auth = original_require_auth

        self.assertEqual(
            response["settings"],
            {
                "enabled": True,
                "max_volume_distance": 4200.0,
                "zero_volume_distance": 18000.0,
            },
        )
        options = main.read_palworld_options()
        self.assertEqual(options["ExpRate"], 2.5)
        self.assertTrue(options["bEnableVoiceChat"])
        self.assertEqual(options["VoiceChatMaxVolumeDistance"], 4200.0)
        self.assertEqual(options["VoiceChatZeroVolumeDistance"], 18000.0)

    def test_zero_volume_distance_cannot_be_shorter_than_full_volume_distance(self):
        original_running_check = main.is_server_container_running
        original_require_auth = main.require_auth
        main.is_server_container_running = lambda: False
        main.require_auth = lambda request: "admin"

        try:
            with self.assertRaises(main.HTTPException) as context:
                main.save_voice_chat(
                    main.VoiceChatSettingsRequest(
                        enabled=True,
                        max_volume_distance=5000,
                        zero_volume_distance=3000,
                    ),
                    request=None,
                )
        finally:
            main.is_server_container_running = original_running_check
            main.require_auth = original_require_auth

        self.assertEqual(context.exception.status_code, 400)

    def test_running_server_keeps_voice_chat_settings_locked(self):
        original_running_check = main.is_server_container_running
        original_require_auth = main.require_auth
        main.is_server_container_running = lambda: True
        main.require_auth = lambda request: "admin"

        try:
            with self.assertRaises(main.HTTPException) as context:
                main.save_voice_chat(
                    main.VoiceChatSettingsRequest(enabled=True),
                    request=None,
                )
        finally:
            main.is_server_container_running = original_running_check
            main.require_auth = original_require_auth

        self.assertEqual(context.exception.status_code, 409)

    def test_dashboard_replaces_discord_mockup_with_voice_chat_menu(self):
        source = Path(main.__file__).read_text(encoding="utf-8")

        self.assertIn('id="voiceChatSettingsBtn"', source)
        self.assertIn('id="voiceChatModal"', source)
        self.assertNotIn('id="discordIntegrationBtn"', source)
        self.assertNotIn('id="discordComingSoonModal"', source)


if __name__ == "__main__":
    unittest.main()
