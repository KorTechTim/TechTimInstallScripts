import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from panels.palworld.app import main


class IniFileEditorTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_data_dir = main.DATA_DIR
        self.original_saved_root_dir = main.SAVED_ROOT_DIR
        self.original_max_bytes = main.INI_EDITOR_MAX_BYTES
        main.DATA_DIR = Path(self.temporary_directory.name)
        main.SAVED_ROOT_DIR = main.DATA_DIR / "server" / "Pal" / "Saved"
        main.INI_EDITOR_MAX_BYTES = 1024
        main.ensure_data_dirs()

    def tearDown(self):
        main.DATA_DIR = self.original_data_dir
        main.SAVED_ROOT_DIR = self.original_saved_root_dir
        main.INI_EDITOR_MAX_BYTES = self.original_max_bytes
        self.temporary_directory.cleanup()

    def create_file(self, name: str, content: str = "value=1\n") -> Path:
        target = main.SAVED_ROOT_DIR / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def test_file_entry_marks_only_ini_files_as_editable(self):
        ini_file = self.create_file("Config/PalWorldSettings.INI")
        text_file = self.create_file("Config/readme.txt")

        self.assertTrue(main.file_entry(ini_file)["editable"])
        self.assertFalse(main.file_entry(text_file)["editable"])

    def test_editor_rejects_non_ini_files(self):
        self.create_file("Config/readme.txt")

        with self.assertRaises(HTTPException) as raised:
            main.resolve_ini_editor_file("Config/readme.txt")

        self.assertEqual(raised.exception.status_code, 400)

    def test_editor_reads_utf8_bom_and_saves_atomically(self):
        target = self.create_file("Config/PalWorldSettings.ini")
        target.write_bytes(b"\xef\xbb\xbfvalue=before\n")

        resolved = main.resolve_ini_editor_file("Config/PalWorldSettings.ini")
        self.assertEqual(main.read_ini_editor_content(resolved), "value=before\n")

        main.write_ini_editor_content(resolved, "value=after\n")

        self.assertEqual(target.read_text(encoding="utf-8"), "value=after\n")
        self.assertEqual(list(target.parent.glob(".*.tmp")), [])

    def test_editor_enforces_size_limit_for_read_and_write(self):
        main.INI_EDITOR_MAX_BYTES = 4
        target = self.create_file("Config/large.ini", "12345")

        with self.assertRaises(HTTPException) as read_error:
            main.read_ini_editor_content(target)
        self.assertEqual(read_error.exception.status_code, 413)

        with self.assertRaises(HTTPException) as write_error:
            main.write_ini_editor_content(target, "12345")
        self.assertEqual(write_error.exception.status_code, 413)


if __name__ == "__main__":
    unittest.main()
