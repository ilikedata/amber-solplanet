from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from amberelectric import AmberApi, ApiClient, Configuration


REQUEST_RETRIES = 2
RETRY_DELAY_SECONDS = 2.0
REQUEST_RESOLUTION_MINUTES = 5


@dataclass(frozen=True)
class AmberPriceSnapshot:
    site_id: str
    start_time: datetime
    end_time: datetime
    duration_minutes: int
    general_per_kwh: float
    general_descriptor: str
    feed_in_per_kwh: float | None
    feed_in_descriptor: str | None
    demand_window: bool


def floor_to_minute(value: datetime) -> datetime:
    return value.astimezone().replace(second=0, microsecond=0)


class AmberClient:
    def __init__(
        self,
        api_key: str,
        request_retries: int = REQUEST_RETRIES,
        retry_delay_seconds: float = RETRY_DELAY_SECONDS,
    ) -> None:
        self.api_key = api_key
        self.request_retries = request_retries
        self.retry_delay_seconds = retry_delay_seconds

    def _with_api(self, callback: Callable[[AmberApi], Any]) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self.request_retries + 1):
            try:
                configuration = Configuration(access_token=self.api_key)
                with ApiClient(configuration) as api_client:
                    return callback(AmberApi(api_client))
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < self.request_retries:
                    time.sleep(self.retry_delay_seconds)
                else:
                    raise RuntimeError(f"Amber API error: {exc}") from exc
        raise RuntimeError(f"Amber API error: {last_exc}")

    def get_price_horizon(self, site_id: str, horizon_hours: int) -> list[AmberPriceSnapshot]:
        horizon_minutes = max(1, horizon_hours * 60)
        request_intervals = max(1, (horizon_minutes + REQUEST_RESOLUTION_MINUTES - 1) // REQUEST_RESOLUTION_MINUTES)
        horizon_end = floor_to_minute(datetime.now().astimezone()) + timedelta(minutes=horizon_minutes)
        intervals = self._with_api(
            lambda api: api.get_current_prices(
                site_id,
                previous=0,
                next=max(request_intervals - 1, 0),
                resolution=REQUEST_RESOLUTION_MINUTES,
            )
        )
        grouped: dict[datetime, dict[str, Any]] = {}
        for interval in intervals:
            actual = interval.actual_instance
            end_time = actual.end_time.astimezone()
            bucket = grouped.setdefault(end_time, {})
            bucket[str(actual.channel_type.value)] = actual

        prices: list[AmberPriceSnapshot] = []
        for end_time in sorted(grouped):
            bucket = grouped[end_time]
            general = bucket.get("general")
            if general is None:
                continue
            feed_in = bucket.get("feedIn")
            demand_window = False
            if general.tariff_information is not None:
                demand_window = bool(general.tariff_information.demand_window)
            prices.append(
                AmberPriceSnapshot(
                    site_id=site_id,
                    start_time=general.start_time.astimezone(),
                    end_time=general.end_time.astimezone(),
                    duration_minutes=int(general.duration),
                    general_per_kwh=float(general.per_kwh),
                    general_descriptor=str(general.descriptor.value),
                    feed_in_per_kwh=None if feed_in is None else float(feed_in.per_kwh),
                    feed_in_descriptor=None if feed_in is None else str(feed_in.descriptor.value),
                    demand_window=demand_window,
                )
            )
        return [price for price in prices if floor_to_minute(price.start_time) < horizon_end]


def build_manual_price_horizon(horizon_hours: int, general_per_kwh: float, now: datetime | None = None) -> list[AmberPriceSnapshot]:
    now = floor_to_minute(now or datetime.now().astimezone())
    prices: list[AmberPriceSnapshot] = []
    interval_count = max(1, horizon_hours * 60)
    for index in range(interval_count):
        start_time = now + timedelta(minutes=index)
        end_time = start_time + timedelta(minutes=1)
        prices.append(
            AmberPriceSnapshot(
                site_id="manual",
                start_time=start_time,
                end_time=end_time,
                duration_minutes=1,
                general_per_kwh=general_per_kwh,
                general_descriptor="manual",
                feed_in_per_kwh=None,
                feed_in_descriptor=None,
                demand_window=False,
            )
        )
    return prices
