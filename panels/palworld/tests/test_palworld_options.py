import re
import tempfile
import unittest
from pathlib import Path

from panels.palworld.app import main


PRIMARY_OPTION_KEYS = {
    "ServerName",
    "ServerDescription",
    "AdminPassword",
    "ServerPassword",
    "PublicPort",
    "ServerPlayerMaxNum",
    "RCONEnabled",
    "RCONPort",
}


class PalworldOptionInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_data_dir = main.DATA_DIR
        main.DATA_DIR = Path(self.temporary_directory.name)

    def tearDown(self):
        main.DATA_DIR = self.original_data_dir
        self.temporary_directory.cleanup()

    def test_schema_contains_all_119_palworld_1_0_template_options(self):
        self.assertEqual(len(main.PALWORLD_OPTION_DEFAULTS), 119)
        self.assertNotIn("AllowConnectPlatform", main.PALWORLD_OPTION_DEFAULTS)
        self.assertEqual(
            main.PALWORLD_ADVANCED_KEYS,
            set(main.PALWORLD_OPTION_DEFAULTS) - PRIMARY_OPTION_KEYS,
        )

    def test_every_advanced_option_is_exposed_in_the_existing_gui(self):
        source = Path(main.__file__).read_text(encoding="utf-8")
        start = source.index("const advancedOptionGroups = [")
        end = source.index("    function getLogBox()", start)
        ui_keys = set(re.findall(r'\{ key: "([^"]+)"', source[start:end]))

        self.assertEqual(ui_keys, main.PALWORLD_ADVANCED_KEYS)

    def test_generated_ini_serializes_all_options_and_empty_technology_list(self):
        config_path = main.write_config(main.default_config())
        contents = config_path.read_text(encoding="utf-8")
        option_text = re.search(r"OptionSettings=\((.*)\)", contents).group(1)
        parsed = main.split_palworld_options(option_text)

        self.assertEqual(set(parsed), set(main.PALWORLD_OPTION_DEFAULTS))
        self.assertIn("DenyTechnologyList=,", contents)
        self.assertNotIn("AllowConnectPlatform", contents)

    def test_deprecated_allow_connect_platform_is_removed_on_import(self):
        config_path = main.get_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "[/Script/Pal.PalGameWorldSettings]\n"
            "OptionSettings=(AllowConnectPlatform=Steam,ServerName=\"Imported\")\n",
            encoding="utf-8",
        )

        options = main.read_palworld_options()

        self.assertNotIn("AllowConnectPlatform", options)
        self.assertEqual(options["ServerName"], "Imported")


if __name__ == "__main__":
    unittest.main()
