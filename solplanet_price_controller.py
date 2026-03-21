#!/usr/bin/env python3
"""Amber forecast-driven Solplanet battery charger."""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from amberelectric import AmberApi, ApiClient, Configuration


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = SCRIPT_DIR / ".env"
DEFAULT_HOST = "192.168.68.119"
DEFAULT_BATTERY_SN = ""
DEFAULT_CHARGE_WATTS = 15000
DEFAULT_PLANNER_CHARGE_KWH_PER_MINUTE = 10.158 / 60.0
DEFAULT_DISCHARGE_WATTS = 1500
DEFAULT_CHARGE_TARGET_SOC = 97
DEFAULT_PRICE_SOURCE = "manual"
DEFAULT_AMBER_SITE_ID = ""
DEFAULT_AMBER_API_KEY = ""
DEFAULT_LOOP_SECONDS = 60
DEFAULT_REQUEST_RETRIES = 2
DEFAULT_RETRY_DELAY_SECONDS = 2.0
DEFAULT_LOG_FILE = "solplanet_price_controller.ndjson"
DEFAULT_PLANNER_HORIZON_HOURS = 24
DEFAULT_BATTERY_CAPACITY_KWH = 50.0
AMBER_REQUEST_RESOLUTION_MINUTES = 5
DEMAND_WINDOW_START_HOUR = 15
DEMAND_WINDOW_END_HOUR = 21
BATTERY_DEVICE = 4
ACTION_SET_BATTERY = "setbattery"
ACTION_SET_DEFINE = "setdefine"
SELF_CONSUMPTION_MODE = 2
CUSTOM_MODE = 4
DAYS = ["Mon", "Tus", "Wen", "Thu", "Fri", "Sat", "Sun"]


@dataclass(frozen=True)
class Slot:
    day: str
    start_hour: int
    start_minute: int
    duration_hours: int
    mode: str

    def to_raw(self) -> int:
        base = 0x3C02
        hour = 0x1000000
        half = 0x1E0000
        duration = 0x3C00
        discharge_flag = 1 if self.mode == "discharge" else 0
        return (
            base
            + (self.start_hour * hour)
            + ((self.start_minute // 30) * half)
            + ((self.duration_hours - 1) * duration)
            + discharge_flag
        )


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


@dataclass(frozen=True)
class BatterySnapshot:
    soc: int
    battery_power_watts: int
    battery_voltage_raw: int
    battery_current_raw: int


@dataclass(frozen=True)
class ControlPlanStep:
    interval_start: datetime
    interval_end: datetime
    action: str
    planned_charge_kwh: float
    planned_charge_minutes: float
    expected_grid_kw: float
    expected_cost: float
    projected_soc: int


@dataclass(frozen=True)
class MinuteBucket:
    parent_end_time: datetime
    minute_start: datetime
    general_per_kwh: float


@dataclass(frozen=True)
class ControlPlan:
    action: str
    steps: list[ControlPlanStep]
    selected_minute_starts: tuple[datetime, ...]
    required_energy_kwh: float
    required_charge_minutes: float
    planned_charge_kwh: float
    planned_charge_minutes: float
    estimated_total_charge_cost: float
    average_planned_charge_price_c_per_kwh: float
    selected_minute_count: int
    target_reachable: bool
    next_charge_at: datetime | None


class InverterUnavailableError(RuntimeError):
    """Raised when the inverter cannot be reached safely."""


class InverterWriteError(RuntimeError):
    """Raised when a write to the inverter fails."""


class PlannerUnavailableError(RuntimeError):
    """Raised when planning inputs are unavailable."""


LOG_FILE_PATH: Path | None = None


def load_dotenv(dotenv_path: Path = DEFAULT_ENV_FILE) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def configure_logging(log_file: str) -> None:
    global LOG_FILE_PATH
    path = Path(log_file).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    LOG_FILE_PATH = path


def log_json(label: str, payload: dict[str, Any]) -> None:
    record = {"ts": datetime.now().astimezone().isoformat(), "event": label, **payload}
    line = json.dumps(record, sort_keys=True)
    if LOG_FILE_PATH is not None:
        with LOG_FILE_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    print(line)


def cents_per_kwh_to_dollars(cents: float) -> float:
    return cents / 100.0


def floor_to_minute(value: datetime) -> datetime:
    return value.astimezone().replace(second=0, microsecond=0)


def current_slot(now: datetime | None = None) -> Slot:
    now = now or datetime.now().astimezone()
    if now.hour == 0 and now.minute < 30:
        return Slot(day=DAYS[now.weekday()], start_hour=0, start_minute=0, duration_hours=1, mode="charge")
    if now.minute < 30:
        return Slot(
            day=DAYS[now.weekday()],
            start_hour=now.hour - 1,
            start_minute=30,
            duration_hours=1,
            mode="charge",
        )
    return Slot(
        day=DAYS[now.weekday()],
        start_hour=now.hour,
        start_minute=0,
        duration_hours=1,
        mode="charge",
    )


def charge_slot_allowed(now: datetime | None = None) -> bool:
    now = now or datetime.now().astimezone()
    slot = current_slot(now)
    slot_start_minutes = (slot.start_hour * 60) + slot.start_minute
    slot_end_minutes = slot_start_minutes + (slot.duration_hours * 60)
    demand_start_minutes = DEMAND_WINDOW_START_HOUR * 60
    demand_end_minutes = DEMAND_WINDOW_END_HOUR * 60
    overlaps_demand = slot_start_minutes < demand_end_minutes and slot_end_minutes > demand_start_minutes
    return not overlaps_demand


def empty_schedule(pin: int = 0, pout: int = 0) -> dict[str, Any]:
    schedule: dict[str, Any] = {"Pin": pin, "Pout": pout}
    for day in DAYS:
        schedule[day] = [0, 0, 0, 0, 0, 0]
    return schedule


def active_schedule(mode: str, watts: int, now: datetime | None = None) -> dict[str, Any]:
    slot = current_slot(now)
    schedule = empty_schedule(pin=watts if mode == "charge" else 0, pout=watts if mode == "discharge" else 0)
    schedule[slot.day][0] = Slot(
        day=slot.day,
        start_hour=slot.start_hour,
        start_minute=slot.start_minute,
        duration_hours=slot.duration_hours,
        mode=mode,
    ).to_raw()
    return schedule


def build_setbattery_payload(battery_info: dict[str, Any], battery_sn: str, mode_register: int) -> dict[str, Any]:
    return {
        "value": {
            "type": battery_info["type"],
            "mod_r": mode_register,
            "sn": battery_sn,
            "discharge_max": battery_info["discharge_max"],
            "charge_max": battery_info["charge_max"],
            "muf": battery_info["muf"],
            "mod": battery_info["mod"],
            "num": battery_info["num"],
        },
        "device": BATTERY_DEVICE,
        "action": ACTION_SET_BATTERY,
    }


def build_setdefine_payload(schedule: dict[str, Any]) -> dict[str, Any]:
    return {"value": schedule, "device": BATTERY_DEVICE, "action": ACTION_SET_DEFINE}


def desired_state(action: str, charge_watts: int, discharge_watts: int) -> tuple[int, dict[str, Any], str]:
    if action == "fallback":
        return SELF_CONSUMPTION_MODE, empty_schedule(), "fallback to self-consumption"
    if action == "charge":
        return CUSTOM_MODE, active_schedule("charge", charge_watts), "charge override"
    if action == "discharge":
        return CUSTOM_MODE, active_schedule("discharge", discharge_watts), "discharge override"
    raise ValueError(f"Unsupported action: {action}")


class SolplanetClient:
    def __init__(
        self,
        host: str,
        timeout_seconds: float = 10.0,
        request_retries: int = DEFAULT_REQUEST_RETRIES,
        retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        self.base_url = f"https://{host}"
        self.timeout_seconds = timeout_seconds
        self.request_retries = request_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

    def get_json(self, endpoint: str) -> dict[str, Any]:
        request = Request(f"{self.base_url}/{endpoint}", method="GET")
        return self._send(request)

    def post_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{self.base_url}/{endpoint}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._send(request)

    def _send(self, request: Request) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self.request_retries + 1):
            try:
                with urlopen(request, timeout=self.timeout_seconds, context=self.ssl_context) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                last_exc = exc
            except URLError as exc:
                last_exc = exc
            if attempt < self.request_retries:
                time.sleep(self.retry_delay_seconds)
        if isinstance(last_exc, HTTPError):
            raise RuntimeError(f"HTTP error from inverter: {last_exc.code}") from last_exc
        if isinstance(last_exc, URLError):
            raise RuntimeError(f"Network error talking to inverter: {last_exc.reason}") from last_exc
        raise RuntimeError("Unknown inverter communication error")


class AmberClient:
    def __init__(
        self,
        api_key: str,
        request_retries: int = DEFAULT_REQUEST_RETRIES,
        retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        self.api_key = api_key
        self.request_retries = request_retries
        self.retry_delay_seconds = retry_delay_seconds

    def _with_api(self, callback: Any) -> Any:
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
        request_intervals = max(1, (horizon_minutes + AMBER_REQUEST_RESOLUTION_MINUTES - 1) // AMBER_REQUEST_RESOLUTION_MINUTES)
        horizon_end = floor_to_minute(datetime.now().astimezone()) + timedelta(minutes=horizon_minutes)
        intervals = self._with_api(
            lambda api: api.get_current_prices(
                site_id,
                previous=0,
                next=max(request_intervals - 1, 0),
                resolution=AMBER_REQUEST_RESOLUTION_MINUTES,
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


def load_battery_snapshot(client: SolplanetClient, battery_sn: str) -> BatterySnapshot:
    try:
        battery_data = client.get_json(f"getdevdata.cgi?device=4&sn={battery_sn}")
        return BatterySnapshot(
            soc=int(battery_data["soc"]),
            battery_power_watts=int(battery_data.get("pb", 0)),
            battery_voltage_raw=int(battery_data.get("vb", 0)),
            battery_current_raw=int(battery_data.get("cb", 0)),
        )
    except Exception as exc:  # noqa: BLE001
        raise InverterUnavailableError(f"Failed to read battery telemetry: {exc}") from exc


def log_battery_snapshot(snapshot: BatterySnapshot) -> None:
    log_json(
        "battery_state",
        {
            "battery_soc": snapshot.soc,
            "battery_power_watts": snapshot.battery_power_watts,
            "battery_voltage_raw": snapshot.battery_voltage_raw,
            "battery_current_raw": snapshot.battery_current_raw,
        },
    )


def build_manual_price_horizon(args: argparse.Namespace) -> list[AmberPriceSnapshot]:
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    interval_delta = timedelta(minutes=1)
    prices: list[AmberPriceSnapshot] = []
    interval_count = max(1, args.planner_horizon_hours * 60)
    for index in range(interval_count):
        start_time = now + (index * interval_delta)
        end_time = start_time + interval_delta
        prices.append(
            AmberPriceSnapshot(
                site_id="manual",
                start_time=start_time,
                end_time=end_time,
                duration_minutes=1,
                general_per_kwh=args.general_per_kwh,
                general_descriptor="manual",
                feed_in_per_kwh=None,
                feed_in_descriptor=None,
                demand_window=False,
            )
        )
    return prices


def build_price_only_charge_plan(
    battery: BatterySnapshot,
    prices: list[AmberPriceSnapshot],
    args: argparse.Namespace,
    now: datetime | None = None,
) -> ControlPlan:
    if not prices:
        raise PlannerUnavailableError("No Amber forecast intervals available")
    if args.battery_capacity_kwh <= 0:
        raise PlannerUnavailableError("Battery capacity must be positive")
    if args.planner_charge_kwh_per_minute <= 0:
        raise PlannerUnavailableError("Planner charge kWh per minute must be positive")

    now = floor_to_minute(now or datetime.now().astimezone())
    current_energy_kwh = (battery.soc / 100.0) * args.battery_capacity_kwh
    target_energy_kwh = (args.charge_target_soc / 100.0) * args.battery_capacity_kwh
    required_energy_kwh = max(0.0, target_energy_kwh - current_energy_kwh)
    required_charge_minutes = required_energy_kwh / args.planner_charge_kwh_per_minute if required_energy_kwh > 0 else 0.0

    minute_buckets: list[MinuteBucket] = []
    for price in prices:
        if price.demand_window:
            continue
        interval_end = floor_to_minute(price.end_time)
        interval_start = interval_end - timedelta(minutes=price.duration_minutes)
        minute_start = max(interval_start, now)
        while minute_start < interval_end:
            minute_buckets.append(
                MinuteBucket(
                    parent_end_time=price.end_time,
                    minute_start=minute_start,
                    general_per_kwh=price.general_per_kwh,
                )
            )
            minute_start += timedelta(minutes=1)

    selected_energy_by_end: dict[datetime, float] = {}
    selected_minutes_by_end: dict[datetime, float] = {}
    selected_minute_buckets: set[datetime] = set()
    next_charge_at: datetime | None = None
    remaining_energy_kwh = required_energy_kwh
    for minute_bucket in sorted(minute_buckets, key=lambda item: (item.general_per_kwh, item.minute_start)):
        if remaining_energy_kwh <= 0:
            break
        selected_kwh = min(args.planner_charge_kwh_per_minute, remaining_energy_kwh)
        if selected_kwh <= 0:
            continue
        selected_energy_by_end[minute_bucket.parent_end_time] = selected_energy_by_end.get(minute_bucket.parent_end_time, 0.0) + selected_kwh
        selected_minutes_by_end[minute_bucket.parent_end_time] = selected_minutes_by_end.get(minute_bucket.parent_end_time, 0.0) + (
            selected_kwh / args.planner_charge_kwh_per_minute
        )
        selected_minute_buckets.add(minute_bucket.minute_start)
        if next_charge_at is None:
            next_charge_at = minute_bucket.minute_start
        remaining_energy_kwh -= selected_kwh

    steps: list[ControlPlanStep] = []
    projected_energy_kwh = current_energy_kwh
    total_planned_charge_kwh = 0.0
    total_planned_charge_minutes = 0.0
    total_cost = 0.0
    for price in prices:
        selected_kwh = selected_energy_by_end.get(price.end_time, 0.0)
        selected_minutes = selected_minutes_by_end.get(price.end_time, 0.0)
        action = "charge" if selected_kwh > 0 else "fallback"
        expected_grid_kw = (args.charge_watts / 1000.0) if action == "charge" else 0.0
        expected_cost = selected_kwh * cents_per_kwh_to_dollars(price.general_per_kwh)
        projected_energy_kwh = min(target_energy_kwh, projected_energy_kwh + selected_kwh)
        projected_soc = int(round((projected_energy_kwh / args.battery_capacity_kwh) * 100.0))
        total_planned_charge_kwh += selected_kwh
        total_planned_charge_minutes += selected_minutes
        total_cost += expected_cost
        steps.append(
            ControlPlanStep(
                interval_start=price.start_time,
                interval_end=price.end_time,
                action=action,
                planned_charge_kwh=round(selected_kwh, 4),
                planned_charge_minutes=round(selected_minutes, 4),
                expected_grid_kw=expected_grid_kw,
                expected_cost=round(expected_cost, 4),
                projected_soc=projected_soc,
            )
        )

    average_price_c_per_kwh = 0.0
    if total_planned_charge_kwh > 0:
        average_price_c_per_kwh = (total_cost / total_planned_charge_kwh) * 100.0

    current_minute_action = "charge" if now in selected_minute_buckets else "fallback"
    sorted_selected_minutes = tuple(sorted(selected_minute_buckets))

    return ControlPlan(
        action=current_minute_action,
        steps=steps,
        selected_minute_starts=sorted_selected_minutes,
        required_energy_kwh=round(required_energy_kwh, 4),
        required_charge_minutes=round(required_charge_minutes, 4),
        planned_charge_kwh=round(total_planned_charge_kwh, 4),
        planned_charge_minutes=round(total_planned_charge_minutes, 4),
        estimated_total_charge_cost=round(total_cost, 2),
        average_planned_charge_price_c_per_kwh=round(average_price_c_per_kwh, 3),
        selected_minute_count=len(selected_minute_buckets),
        target_reachable=total_planned_charge_kwh + 1e-6 >= required_energy_kwh,
        next_charge_at=next_charge_at,
    )


def build_hourly_plan_preview(
    steps: list[ControlPlanStep],
    selected_minute_starts: tuple[datetime, ...],
    next_charge_at: datetime | None,
    hours: int = 24,
) -> tuple[str, list[int], str | None]:
    if not steps:
        return "", [], None
    start_hour = steps[0].interval_start.replace(minute=0, second=0, microsecond=0)
    hourly_actions = [0] * hours
    for minute_start in selected_minute_starts:
        hour_index = int((minute_start - start_hour).total_seconds() // 3600)
        if 0 <= hour_index < hours:
            hourly_actions[hour_index] = 1
    return start_hour.isoformat(), hourly_actions, None if next_charge_at is None else next_charge_at.isoformat()


def apply_state(
    client: SolplanetClient,
    battery_sn: str,
    action: str,
    charge_watts: int,
    discharge_watts: int,
    apply: bool,
) -> None:
    battery_info = client.get_json(f"getdev.cgi?device=4&sn={battery_sn}")
    mode_register, schedule, description = desired_state(action, charge_watts, discharge_watts)
    mode_payload = build_setbattery_payload(battery_info, battery_sn, mode_register)
    schedule_payload = build_setdefine_payload(schedule)

    if not apply:
        log_json("dry_run", {"action": action, "description": description})
        return

    try:
        schedule_response = client.post_json("setting.cgi", schedule_payload)
        mode_response = client.post_json("setting.cgi", mode_payload)
    except Exception as exc:  # noqa: BLE001
        raise InverterWriteError(f"Failed to write inverter state: {exc}") from exc

    try:
        confirmed_mode = client.get_json(f"getdev.cgi?device=4&sn={battery_sn}")
        confirmed_schedule = client.get_json("getdefine.cgi")
    except Exception as exc:  # noqa: BLE001
        raise InverterUnavailableError(f"Failed to confirm inverter state after write: {exc}") from exc

    log_json(
        "applied",
        {
            "action": action,
            "description": description,
            "schedule_response": schedule_response.get("dat"),
            "mode_response": mode_response.get("dat"),
            "confirmed_mod_r": confirmed_mode.get("mod_r"),
            "confirmed_pin": confirmed_schedule.get("Pin"),
            "confirmed_pout": confirmed_schedule.get("Pout"),
        },
    )


def run_once(args: argparse.Namespace) -> None:
    client = SolplanetClient(host=args.host)
    battery = load_battery_snapshot(client, args.battery_sn)
    log_battery_snapshot(battery)
    now = datetime.now().astimezone()

    try:
        if args.price_source == "amber":
            amber_api_key = env_or_default("AMBER_API_KEY", DEFAULT_AMBER_API_KEY)
            if not amber_api_key:
                raise PlannerUnavailableError("AMBER_API_KEY is not configured")
            amber_client = AmberClient(api_key=amber_api_key)
            prices = amber_client.get_price_horizon(
                site_id=args.amber_site_id,
                horizon_hours=args.planner_horizon_hours,
            )
            source = "amber_price_plan"
        else:
            prices = build_manual_price_horizon(args)
            source = "manual_price_plan"

        plan = build_price_only_charge_plan(battery=battery, prices=prices, args=args, now=now)
        current_interval = prices[0]
        derived_action = plan.action
        final_action = derived_action
        if derived_action == "charge" and not charge_slot_allowed():
            final_action = "fallback"

        plan_hourly_start, plan_hourly_actions, next_charge_at = build_hourly_plan_preview(
            plan.steps,
            plan.selected_minute_starts,
            plan.next_charge_at,
        )

        log_json(
            "decision",
            {
                "source": source,
                "battery_soc": battery.soc,
                "forecast_general_per_kwh": current_interval.general_per_kwh,
                "forecast_feed_in_per_kwh": current_interval.feed_in_per_kwh,
                "planner_charge_kwh_per_minute": round(args.planner_charge_kwh_per_minute, 6),
                "command_charge_watts": args.charge_watts,
                "projected_soc": plan.steps[0].projected_soc,
                "derived_action": derived_action,
                "final_action": final_action,
            },
        )
        log_json(
            "plan_preview",
            {
                "source": source,
                "current_action": final_action,
                "feasible_plan_through": plan.steps[-1].interval_end.isoformat(),
                "plan_hourly_start": plan_hourly_start,
                "plan_hourly_actions": plan_hourly_actions,
                "next_charge_at": next_charge_at,
                "required_energy_kwh": plan.required_energy_kwh,
                "required_charge_minutes": plan.required_charge_minutes,
                "estimated_total_charge_kwh": plan.planned_charge_kwh,
                "estimated_total_charge_minutes": plan.planned_charge_minutes,
                "estimated_total_charge_cost": plan.estimated_total_charge_cost,
                "average_planned_charge_price_c_per_kwh": plan.average_planned_charge_price_c_per_kwh,
                "selected_minute_count": plan.selected_minute_count,
                "target_reachable": plan.target_reachable,
            },
        )
        action = final_action
    except Exception as exc:  # noqa: BLE001
        action = "fallback"
        log_json(
            "planner_fallback",
            {
                "battery_soc": battery.soc,
                "message": str(exc),
                "fallback_action": action,
            },
        )

    apply_state(
        client=client,
        battery_sn=args.battery_sn,
        action=action,
        charge_watts=args.charge_watts,
        discharge_watts=args.discharge_watts,
        apply=args.apply,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Amber forecast-driven Solplanet battery charger.")
    parser.add_argument("--price-source", choices=["manual", "amber"], default=DEFAULT_PRICE_SOURCE)
    parser.add_argument("--general-per-kwh", type=float, default=0.0, help="Manual import price in c/kWh.")
    parser.add_argument("--host", default=env_or_default("SOLPLANET_HOST", DEFAULT_HOST), help="Solplanet inverter host.")
    parser.add_argument("--battery-sn", default=env_or_default("SOLPLANET_BATTERY_SN", DEFAULT_BATTERY_SN))
    parser.add_argument(
        "--charge-watts",
        type=int,
        default=DEFAULT_CHARGE_WATTS,
        help="Grid-charge command power in watts, including house-load headroom.",
    )
    parser.add_argument(
        "--planner-charge-kwh-per-minute",
        type=float,
        default=DEFAULT_PLANNER_CHARGE_KWH_PER_MINUTE,
        help="Battery charge rate normalized to kWh per minute for planning.",
    )
    parser.add_argument(
        "--discharge-watts",
        type=int,
        default=DEFAULT_DISCHARGE_WATTS,
        help="Unused by the minimal planner; retained for inverter state application.",
    )
    parser.add_argument("--charge-target-soc", type=int, default=DEFAULT_CHARGE_TARGET_SOC)
    parser.add_argument("--battery-capacity-kwh", type=float, default=DEFAULT_BATTERY_CAPACITY_KWH)
    parser.add_argument("--planner-horizon-hours", type=int, default=DEFAULT_PLANNER_HORIZON_HOURS)
    parser.add_argument("--amber-site-id", default=env_or_default("AMBER_SITE_ID", DEFAULT_AMBER_SITE_ID))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--loop-seconds", type=int, default=DEFAULT_LOOP_SECONDS)
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    load_dotenv()
    args = parse_args(argv)
    configure_logging(args.log_file)
    try:
        if not args.loop:
            run_once(args)
            return 0
        while True:
            try:
                run_once(args)
            except InverterUnavailableError as exc:
                log_json("error", {"cycle_error": "inverter_unavailable", "message": str(exc), "next_action": "retry_next_cycle"})
            except InverterWriteError as exc:
                log_json("error", {"cycle_error": "inverter_write_failed", "message": str(exc), "next_action": "retry_next_cycle"})
            except Exception as exc:  # noqa: BLE001
                log_json("error", {"cycle_error": "unexpected", "message": str(exc), "next_action": "retry_next_cycle"})
            time.sleep(args.loop_seconds)
    except KeyboardInterrupt:
        log_json("stopped", {"message": "Stopped by user"})
        return 0
    except Exception as exc:  # noqa: BLE001
        log_json("fatal_error", {"message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
