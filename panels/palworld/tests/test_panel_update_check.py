import unittest
from unittest.mock import patch

from panels.palworld.app import main


class FakeImage:
    def __init__(self, digest):
        self.id = "sha256:local-image"
        self.attrs = {"RepoDigests": [f"{main.PANEL_IMAGE.split(':')[0]}@{digest}"]}

    def reload(self):
        return None


class FakeContainer:
    def __init__(self, digest):
        self.image = FakeImage(digest)

    def reload(self):
        return None


class FakeDockerClient:
    def __init__(self, current_digest, latest_digest):
        self.current_digest = current_digest
        self.latest_digest = latest_digest
        self.containers = self
        self.images = self

    def get(self, name):
        self.requested_container = name
        return FakeContainer(self.current_digest)

    def get_registry_data(self, image):
        self.requested_image = image
        return type("RegistryData", (), {"id": self.latest_digest})()


class PanelUpdateCheckTests(unittest.TestCase):
    def setUp(self):
        self.original_cache = dict(main.PANEL_UPDATE_CHECK_CACHE)
        main.PANEL_UPDATE_CHECK_CACHE.update({"expires_at": 0.0, "payload": None})

    def tearDown(self):
        main.PANEL_UPDATE_CHECK_CACHE.clear()
        main.PANEL_UPDATE_CHECK_CACHE.update(self.original_cache)

    def test_reports_no_update_when_registry_digest_matches(self):
        client = FakeDockerClient("sha256:same", "sha256:same")

        with patch.object(main.docker, "from_env", return_value=client):
            payload = main.panel_update_check_payload()

        self.assertEqual(payload["status"], "ok")
        self.assertFalse(payload["update_available"])
        self.assertEqual(client.requested_container, main.PANEL_CONTAINER_NAME)
        self.assertEqual(client.requested_image, main.PANEL_IMAGE)

    def test_reports_update_when_registry_digest_differs(self):
        client = FakeDockerClient("sha256:current", "sha256:new")

        with patch.object(main.docker, "from_env", return_value=client):
            payload = main.panel_update_check_payload()

        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["update_available"])

    def test_registry_failure_does_not_show_false_update_notice(self):
        with patch.object(main.docker, "from_env", side_effect=RuntimeError("registry offline")):
            payload = main.panel_update_check_payload()

        self.assertEqual(payload["status"], "unavailable")
        self.assertFalse(payload["update_available"])
        self.assertIn("registry offline", payload["message"])


if __name__ == "__main__":
    unittest.main()
