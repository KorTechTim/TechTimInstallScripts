import tempfile
import unittest
from pathlib import Path

from panels.palworld.app import main


class ServerResourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.original_paths = {
            "PROC_STAT_FILE": main.PROC_STAT_FILE,
            "PROC_MEMINFO_FILE": main.PROC_MEMINFO_FILE,
        }
        main.PROC_STAT_FILE = self.root / "stat"
        main.PROC_MEMINFO_FILE = self.root / "meminfo"
        main.OS_CPU_SAMPLE.clear()

    def tearDown(self):
        for name, value in self.original_paths.items():
            setattr(main, name, value)
        main.OS_CPU_SAMPLE.clear()
        self.temporary_directory.cleanup()

    def test_cpu_usage_reports_each_logical_cpu(self):
        main.PROC_STAT_FILE.write_text(
            "cpu  20 0 10 70 0 0 0 0\n"
            "cpu0 10 0 5 35 0 0 0 0\n"
            "cpu1 10 0 5 35 0 0 0 0\n",
            encoding="ascii",
        )
        main.os_cpu_usage()
        main.PROC_STAT_FILE.write_text(
            "cpu  50 0 20 130 0 0 0 0\n"
            "cpu0 30 0 10 60 0 0 0 0\n"
            "cpu1 20 0 10 70 0 0 0 0\n",
            encoding="ascii",
        )

        average, threads = main.os_cpu_usage()

        self.assertEqual([item["thread"] for item in threads], [1, 2])
        self.assertEqual([item["percent"] for item in threads], [50.0, 30.0])
        self.assertEqual(average, 40.0)

    def test_memory_usage_uses_available_memory_and_swap(self):
        main.PROC_MEMINFO_FILE.write_text(
            "MemTotal:       1000000 kB\n"
            "MemAvailable:    250000 kB\n"
            "SwapTotal:       200000 kB\n"
            "SwapFree:         50000 kB\n",
            encoding="ascii",
        )

        memory = main.os_memory_usage()

        self.assertEqual(memory["memory_percent"], 75.0)
        self.assertEqual(memory["memory_used"], 750000 * 1024)
        self.assertEqual(memory["memory_available"], 250000 * 1024)
        self.assertEqual(memory["swap_used"], 150000 * 1024)


if __name__ == "__main__":
    unittest.main()
