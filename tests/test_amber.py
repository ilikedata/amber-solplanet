import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from amber import AmberClient, build_manual_price_horizon


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_interval(item: dict) -> SimpleNamespace:
    actual = SimpleNamespace(
        start_time=dt(item["start_time"]),
        end_time=dt(item["end_time"]),
        duration=item["duration"],
        per_kwh=item["per_kwh"],
        descriptor=SimpleNamespace(value=item["descriptor"]),
        channel_type=SimpleNamespace(value=item["channel_type"]),
        tariff_information=SimpleNamespace(demand_window=item["demand_window"]),
    )
    return SimpleNamespace(actual_instance=actual)


class AmberClientTests(unittest.TestCase):
    def test_with_api_retries_then_succeeds(self) -> None:
        client = AmberClient(api_key="token", request_retries=2, retry_delay_seconds=0)
        attempts = {"count": 0}

        def callback(_api):
            attempts["count"] += 1
            if attempts["count"] < 2:
                raise RuntimeError("temporary")
            return "ok"

        result = client._with_api(callback)

        self.assertEqual(result, "ok")
        self.assertEqual(attempts["count"], 2)

    def test_with_api_raises_after_retry_exhaustion(self) -> None:
        client = AmberClient(api_key="token", request_retries=1, retry_delay_seconds=0)

        with self.assertRaises(RuntimeError) as ctx:
            client._with_api(lambda _api: (_ for _ in ()).throw(RuntimeError("boom")))

        self.assertIn("Amber API error", str(ctx.exception))

    def test_get_price_horizon_groups_channels_and_trims_to_horizon(self) -> None:
        intervals = [make_interval(item) for item in load_fixture("amber_current_prices.json")]
        fixed_now = dt("2026-03-21T22:00:00+11:00")
        client = AmberClient(api_key="token")

        with patch.object(client, "_with_api", return_value=intervals):
            with patch("amber.datetime") as mock_datetime:
                mock_datetime.now.return_value = fixed_now
                prices = client.get_price_horizon(site_id="site", horizon_hours=1)

        self.assertEqual(len(prices), 3)
        self.assertEqual(prices[0].feed_in_per_kwh, -8.08603)
        self.assertIsNone(prices[1].feed_in_per_kwh)
        self.assertTrue(prices[2].demand_window)
        self.assertEqual(prices[-1].end_time, dt("2026-03-21T22:15:00+11:00"))

    def test_get_price_horizon_skips_buckets_without_general_channel(self) -> None:
        intervals = [
            make_interval(item)
            for item in [
                {
                    "channel_type": "feedIn",
                    "start_time": "2026-03-21T22:00:01+11:00",
                    "end_time": "2026-03-21T22:05:00+11:00",
                    "duration": 5,
                    "per_kwh": -7.0,
                    "descriptor": "high",
                    "demand_window": False,
                }
            ]
        ]
        client = AmberClient(api_key="token")

        with patch.object(client, "_with_api", return_value=intervals):
            with patch("amber.datetime") as mock_datetime:
                mock_datetime.now.return_value = dt("2026-03-21T22:00:00+11:00")
                prices = client.get_price_horizon(site_id="site", horizon_hours=1)

        self.assertEqual(prices, [])

    def test_get_price_horizon_defaults_demand_window_when_tariff_information_missing(self) -> None:
        item = make_interval(load_fixture("amber_current_prices.json")[0])
        item.actual_instance.tariff_information = None
        client = AmberClient(api_key="token")

        with patch.object(client, "_with_api", return_value=[item]):
            with patch("amber.datetime") as mock_datetime:
                mock_datetime.now.return_value = dt("2026-03-21T22:00:00+11:00")
                prices = client.get_price_horizon(site_id="site", horizon_hours=1)

        self.assertEqual(len(prices), 1)
        self.assertFalse(prices[0].demand_window)

    def test_build_manual_price_horizon_creates_one_minute_buckets(self) -> None:
        now = dt("2026-03-21T22:00:00+11:00")

        prices = build_manual_price_horizon(horizon_hours=1, general_per_kwh=12.5, now=now)

        self.assertEqual(len(prices), 60)
        self.assertEqual(prices[0].start_time, now)
        self.assertEqual(prices[0].duration_minutes, 1)
        self.assertEqual(prices[-1].end_time, dt("2026-03-21T23:00:00+11:00"))


if __name__ == "__main__":
    unittest.main()
