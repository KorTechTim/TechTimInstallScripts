from datetime import datetime, timezone
import json
import unittest
from unittest.mock import MagicMock, patch

from app.discord_webhook import (
    build_webhook_payload,
    execute_webhook,
    masked_webhook_url,
    normalize_webhook_url,
    webhook_execute_url,
)


WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz_ABCDEFG-123456"


class DiscordWebhookTests(unittest.TestCase):
    def test_normalizes_official_webhook(self):
        self.assertEqual(normalize_webhook_url(f"{WEBHOOK}/"), WEBHOOK)

    def test_rejects_non_discord_url(self):
        with self.assertRaises(ValueError):
            normalize_webhook_url("https://example.com/api/webhooks/123/token")

    def test_masks_secret_token(self):
        masked = masked_webhook_url(WEBHOOK)
        self.assertEqual(masked, "Discord 웹훅 · 1234...5678")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", masked)

    def test_execute_url_requests_confirmation(self):
        self.assertEqual(webhook_execute_url(WEBHOOK), f"{WEBHOOK}?wait=true")

    def test_payload_disables_mentions_and_limits_fields(self):
        payload = build_webhook_payload(
            username="TechTim",
            title="서버 시작",
            description="@everyone 서버가 시작되었습니다.",
            color=0x55AA55,
            fields=[{"name": "서버", "value": "테스트", "inline": True}] * 30,
            timestamp=datetime(2026, 7, 19, tzinfo=timezone.utc),
        )
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(len(payload["embeds"][0]["fields"]), 25)
        self.assertEqual(payload["embeds"][0]["timestamp"], "2026-07-19T00:00:00+00:00")

    @patch("app.discord_webhook.urlopen")
    def test_execute_webhook_posts_json_and_waits_for_result(self, mocked_urlopen):
        response = MagicMock()
        response.status = 204
        mocked_urlopen.return_value.__enter__.return_value = response
        payload = {"content": "test", "allowed_mentions": {"parse": []}}

        execute_webhook(WEBHOOK, payload)

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, f"{WEBHOOK}?wait=true")
        self.assertEqual(json.loads(request.data), payload)
        self.assertEqual(request.get_method(), "POST")


if __name__ == "__main__":
    unittest.main()
