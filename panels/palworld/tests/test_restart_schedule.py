import json
import tempfile
import unittest
from datetime import datetime as RealDateTime
from pathlib import Path
from unittest.mock import patch

from panels.palworld.app import main


class FixedDateTime:
    current = RealDateTime(2026, 7, 19, 4, 0, tzinfo=main.KST)

    @classmethod
    def now(cls, timezone=None):
        return cls.current


class FakeContainer:
    status = "running"

    def __init__(self):
        self.restart_calls = 0

    def reload(self):
        return None

    def restart(self, timeout):
        self.restart_calls += 1


class FakeDockerClient:
    def __init__(self, container):
        self.container = container
        self.containers = self

    def get(self, name):
        self.requested_name = name
        return self.container


class RestartScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.original_paths = {
            "DATA_DIR": main.DATA_DIR,
            "SAVED_ROOT_DIR": main.SAVED_ROOT_DIR,
            "RESTART_SCHEDULE_FILE": main.RESTART_SCHEDULE_FILE,
            "SERVER_CONTROL_LOG_FILE": main.SERVER_CONTROL_LOG_FILE,
        }
        main.DATA_DIR = self.root
        main.SAVED_ROOT_DIR = self.root / "server" / "Pal" / "Saved"
        main.RESTART_SCHEDULE_FILE = self.root / "restart-schedule.json"
        main.SERVER_CONTROL_LOG_FILE = self.root / "server-control.log"

    def tearDown(self):
        for name, value in self.original_paths.items():
            setattr(main, name, value)
        self.temporary_directory.cleanup()

    def test_load_migrates_existing_single_restart_time(self):
        main.RESTART_SCHEDULE_FILE.write_text(
            json.dumps({"enabled": True, "restart_time": "06:30"}),
            encoding="utf-8",
        )

        schedule = main.load_restart_schedule()

        self.assertEqual(schedule["restart_times"], ["06:30"])
        self.assertEqual(schedule["restart_time"], "06:30")

    def test_restart_times_are_sorted_deduplicated_and_limited(self):
        self.assertEqual(
            main.normalize_restart_times(["20:00", "04:00", "12:00", "04:00"]),
            ["04:00", "12:00", "20:00"],
        )

        with self.assertRaises(ValueError):
            main.normalize_restart_times(["01:00", "02:00", "03:00", "04:00"])

    def test_each_configured_time_runs_once_per_day(self):
        main.persist_restart_schedule({
            "enabled": True,
            "restart_times": ["04:00", "12:00"],
        })
        container = FakeContainer()
        client = FakeDockerClient(container)

        with (
            patch.object(main, "datetime", FixedDateTime),
            patch.object(main.docker, "from_env", return_value=client),
            patch.object(main, "server_container_uses_official_runtime", return_value=True),
        ):
            FixedDateTime.current = RealDateTime(2026, 7, 19, 4, 0, tzinfo=main.KST)
            main.run_scheduled_restart_if_due()
            main.run_scheduled_restart_if_due()
            FixedDateTime.current = RealDateTime(2026, 7, 19, 12, 0, tzinfo=main.KST)
            main.run_scheduled_restart_if_due()
            main.run_scheduled_restart_if_due()

        self.assertEqual(container.restart_calls, 2)
        self.assertEqual(
            main.load_restart_schedule()["last_run_key"],
            "2026-07-19|12:00",
        )


if __name__ == "__main__":
    unittest.main()
