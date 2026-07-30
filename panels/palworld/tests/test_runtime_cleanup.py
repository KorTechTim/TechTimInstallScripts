import unittest

from panels.palworld.app import main


class FakeImage:
    def __init__(self, image_id):
        self.id = image_id


class FakeContainer:
    def __init__(self, status, image_id):
        self.status = status
        self.image = FakeImage(image_id)
        self.removed = False

    def reload(self):
        return None

    def remove(self, force=False):
        self.removed = force


class FakeContainers:
    def __init__(self, container=None):
        self.container = container

    def get(self, name):
        if self.container is None:
            raise main.docker.errors.NotFound("missing")
        self.requested_name = name
        return self.container


class FakeImages:
    def __init__(self, reclaimed=0, deleted=None):
        self.reclaimed = reclaimed
        self.deleted = deleted or []
        self.filters = None

    def prune(self, filters=None):
        self.filters = filters
        return {
            "ImagesDeleted": self.deleted,
            "SpaceReclaimed": self.reclaimed,
        }


class FakeClient:
    def __init__(self, container=None, reclaimed=0, deleted=None):
        self.containers = FakeContainers(container)
        self.images = FakeImages(reclaimed, deleted)


class RuntimeCleanupTests(unittest.TestCase):
    def test_removes_only_stopped_game_container_using_old_image(self):
        container = FakeContainer("exited", "sha256:old")
        client = FakeClient(
            container,
            reclaimed=3 * 1024 * 1024 * 1024,
            deleted=[{"Deleted": "sha256:old"}],
        )

        result = main.cleanup_obsolete_palworld_runtime_data(client, "sha256:new")

        self.assertTrue(container.removed)
        self.assertTrue(result["container_removed"])
        self.assertEqual(result["images_deleted"], 1)
        self.assertEqual(result["space_reclaimed"], 3 * 1024 * 1024 * 1024)
        self.assertEqual(client.images.filters, {"dangling": True})

    def test_keeps_container_when_it_already_uses_current_image(self):
        container = FakeContainer("exited", "sha256:current")
        client = FakeClient(container)

        result = main.cleanup_obsolete_palworld_runtime_data(client, "sha256:current")

        self.assertFalse(container.removed)
        self.assertFalse(result["container_removed"])

    def test_never_removes_running_game_container(self):
        container = FakeContainer("running", "sha256:old")
        client = FakeClient(container)

        result = main.cleanup_obsolete_palworld_runtime_data(client, "sha256:new")

        self.assertFalse(container.removed)
        self.assertFalse(result["container_removed"])

    def test_cleanup_succeeds_without_existing_game_container(self):
        client = FakeClient(reclaimed=1024)

        result = main.cleanup_obsolete_palworld_runtime_data(client, "sha256:new")

        self.assertFalse(result["container_removed"])
        self.assertEqual(result["space_reclaimed"], 1024)

    def test_formats_reclaimed_storage_for_logs(self):
        self.assertEqual(main.format_storage_bytes(0), "0 B")
        self.assertEqual(main.format_storage_bytes(1536), "1.5 KB")
        self.assertEqual(main.format_storage_bytes(3 * 1024**3), "3.0 GB")


if __name__ == "__main__":
    unittest.main()
