from datetime import datetime, timezone
import unittest

from app.game_metrics import (
    container_uptime_seconds,
    health_state,
    parse_forge_tps,
    parse_paper_mspt,
    parse_paper_tps,
    parse_player_counts,
    parse_spark_tps,
    parse_tick_query,
    version_at_least,
)


class GameMetricsParserTests(unittest.TestCase):
    def test_player_counts(self):
        self.assertEqual(
            parse_player_counts("There are 2 of a max of 20 players online: Alex, Steve"),
            (2, 20),
        )

    def test_paper_tps(self):
        metrics = parse_paper_tps("TPS from last 1m, 5m, 15m: 19.98, 19.95, 19.90")
        self.assertEqual(metrics["tps"], 19.98)
        self.assertEqual(metrics["tps_15m"], 19.9)

    def test_paper_mspt(self):
        output = "Server tick times (avg/min/max) from last 5s, 10s, 1m:\n◴ 12.4/4.0/29.1, 11.8/3.8/31.2, 10.2/3.4/35.0"
        metrics = parse_paper_mspt(output)
        self.assertEqual(metrics["mspt"], 12.4)
        self.assertEqual(metrics["mspt_max"], 29.1)

    def test_spark_tps_and_tick_durations(self):
        output = """[spark] TPS from last 5s, 10s, 1m, 5m, 15m:
[spark] 19.99, 19.98, 19.97, 19.96, 19.95
[spark] Tick durations (min/med/95%ile/max ms) from last 10s, 1m:
[spark] 2.0/8.0/18.0/32.0; 2.5/9.0/20.0/36.0
"""
        metrics = parse_spark_tps(output)
        self.assertEqual(metrics["tps"], 19.99)
        self.assertEqual(metrics["tps_1m"], 19.97)
        self.assertEqual(metrics["mspt"], 8.0)
        self.assertEqual(metrics["mspt_p95"], 18.0)

    def test_forge_tps_uses_overall_last_line(self):
        output = """Dim minecraft:overworld: Mean tick time: 12.0 ms. Mean TPS: 20.0
Overall: Mean tick time: 14.5 ms. Mean TPS: 19.2
"""
        self.assertEqual(parse_forge_tps(output), {"tps": 19.2, "mspt": 14.5})

    def test_tick_query_derives_tps_from_average_tick_time(self):
        output = "The game is running normally at 20.0 TPS with an average tick time of 62.5 ms."
        self.assertEqual(parse_tick_query(output), {"tps": 16.0, "mspt": 62.5})

    def test_version_comparison(self):
        self.assertTrue(version_at_least("1.20.3", (1, 20, 3)))
        self.assertTrue(version_at_least("LATEST", (1, 20, 3)))
        self.assertFalse(version_at_least("1.20.1", (1, 20, 3)))

    def test_uptime_and_health(self):
        now = datetime(2026, 7, 19, 6, 0, tzinfo=timezone.utc)
        self.assertEqual(container_uptime_seconds("2026-07-19T05:00:00Z", now), 3600)
        self.assertEqual(health_state({"tps": 19.9, "mspt": 20}), "healthy")
        self.assertEqual(health_state({"tps": 18.5, "mspt": 45}), "warning")
        self.assertEqual(health_state({"tps": 15, "mspt": 70}), "critical")


if __name__ == "__main__":
    unittest.main()
