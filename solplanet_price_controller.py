#!/usr/bin/env python3
"""Amber forecast-driven Solplanet battery charger."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from amber import AmberClient, AmberPriceSnapshot, build_manual_price_horizon, floor_to_minute
from planner import PlannerUnavailableError, next_demand_window_start
from solplanet import (
    InverterUnavailableError,
    InverterWriteError,
    SolplanetClient,
    apply_state,
    charge_slot_allowed,
    load_battery_snapshot_with_telemetry,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = SCRIPT_DIR / ".env"
DEFAULT_HOST = "192.168.68.119"
DEFAULT_BATTERY_SN = ""
DEFAULT_CHARGE_WATTS = 15000
DEFAULT_PLANNER_CHARGE_KWH_PER_MINUTE = 10.158 / 60.0
DEFAULT_DISCHARGE_WATTS = 15000
DEFAULT_CHARGE_TARGET_SOC = 97
DEFAULT_PRICE_SOURCE = "manual"
DEFAULT_AMBER_SITE_ID = ""
DEFAULT_AMBER_API_KEY = ""
DEFAULT_LOOP_SECONDS = 60
DEFAULT_LOG_FILE = "solplanet_price_controller.ndjson"
DEFAULT_AMBER_FORECAST_LOG = "amber_forecast.ndjson"
DEFAULT_PLANNER_HORIZON_HOURS = 24
DEFAULT_BATTERY_CAPACITY_KWH = 50.0
DEFAULT_LATENESS_PENALTY_START_HOUR = 13
DEFAULT_MAX_LATENESS_PENALTY_C_PER_KWH = 1.5
DEFAULT_FORECAST_RISK_HORIZON_HOURS = 6
DEFAULT_MAX_FORECAST_RISK_PENALTY_C_PER_KWH = 1.0
DEFAULT_MAX_CHARGE_PRICE_C_PER_KWH = 10.0
DEFAULT_AFTERNOON_MAX_CHARGE_PRICE_C_PER_KWH = 11.0
DEFAULT_REDUCED_CHARGE_WATTS = 0
DEFAULT_DISCHARGE_MIN_SOC = 55
DEFAULT_DISCHARGE_FEED_IN_THRESHOLD_C_PER_KWH = 16.0
DEFAULT_DISCHARGE_CHEAP_LOOKAHEAD_HOURS = 24
DEFAULT_DISCHARGE_CHEAP_PRICE_THRESHOLD_C_PER_KWH = 10.0
DEFAULT_DISCHARGE_REQUIRED_CHEAP_HOURS = 4.0


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


def log_amber_forecast(log_file: str, prices: list[AmberPriceSnapshot]) -> None:
    path = Path(log_file).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    record = {
        "ts": datetime.now().astimezone().isoformat(),
        "event": "amber_forecast_fetch",
        "prices": [p.to_dict() for p in prices],
    }
    line = json.dumps(record, sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def feed_in_credit_c_per_kwh(feed_in_per_kwh: float | None) -> float | None:
    if feed_in_per_kwh is None:
        return None
    return -feed_in_per_kwh if feed_in_per_kwh < 0 else feed_in_per_kwh


def cheap_charge_minutes_within_window(
    prices: list[AmberPriceSnapshot],
    now: datetime,
    cheap_price_threshold_c_per_kwh: float,
    lookahead_hours: int,
) -> int:
    if lookahead_hours <= 0:
        return 0
    window_start = floor_to_minute(now)
    window_end = min(window_start + timedelta(hours=lookahead_hours), next_demand_window_start(window_start))
    cheap_minutes = 0
    for price in prices:
        if price.demand_window or price.general_per_kwh > cheap_price_threshold_c_per_kwh:
            continue
        interval_start = max(floor_to_minute(price.start_time), window_start)
        interval_end = min(floor_to_minute(price.end_time), window_end)
        if interval_start >= interval_end:
            continue
        cheap_minutes += int((interval_end - interval_start).total_seconds() // 60)
    return cheap_minutes


def should_discharge_now(
    args: argparse.Namespace,
    battery_soc: int,
    prices: list[AmberPriceSnapshot],
    current_interval: AmberPriceSnapshot,
    now: datetime,
) -> tuple[bool, float | None, int]:
    feed_in_credit = feed_in_credit_c_per_kwh(current_interval.feed_in_per_kwh)
    cheap_minutes = cheap_charge_minutes_within_window(
        prices=prices,
        now=now,
        cheap_price_threshold_c_per_kwh=args.discharge_cheap_price_threshold_c_per_kwh,
        lookahead_hours=args.discharge_cheap_lookahead_hours,
    )
    required_cheap_minutes = int(args.discharge_required_cheap_hours * 60)
    if args.price_source != "amber":
        return False, feed_in_credit, cheap_minutes
    if args.planner_horizon_hours < args.discharge_cheap_lookahead_hours:
        return False, feed_in_credit, cheap_minutes
    if battery_soc <= args.discharge_min_soc:
        return False, feed_in_credit, cheap_minutes
    if feed_in_credit is None or feed_in_credit <= args.discharge_feed_in_threshold_c_per_kwh:
        return False, feed_in_credit, cheap_minutes
    if cheap_minutes < required_cheap_minutes:
        return False, feed_in_credit, cheap_minutes
    return True, feed_in_credit, cheap_minutes


def desired_charge_watts(
    current_interval: AmberPriceSnapshot,
    battery_soc: int,
    charge_target_soc: int,
    max_charge_watts: int,
    base_max_charge_price_c_per_kwh: float,
    afternoon_max_charge_price_c_per_kwh: float = DEFAULT_AFTERNOON_MAX_CHARGE_PRICE_C_PER_KWH,
    reduced_charge_watts: int = DEFAULT_REDUCED_CHARGE_WATTS,
) -> int:
    if current_interval.demand_window or battery_soc >= charge_target_soc:
        return 0
    current_price = current_interval.general_per_kwh
    if current_price <= base_max_charge_price_c_per_kwh:
        return max_charge_watts
    if current_price <= afternoon_max_charge_price_c_per_kwh:
        return min(reduced_charge_watts, max_charge_watts)
    return 0


def run_once(args: argparse.Namespace) -> None:
    client = SolplanetClient(host=args.host)
    battery, telemetry = load_battery_snapshot_with_telemetry(client, args.battery_sn)
    log_json(
        "battery_state",
        {
            "battery_soc": battery.soc,
            "battery_power_watts": battery.battery_power_watts,
            "battery_voltage_raw": battery.battery_voltage_raw,
            "battery_current_raw": battery.battery_current_raw,
            "inverter_telemetry": telemetry,
        },
    )
    now = datetime.now().astimezone()

    try:
        if args.price_source == "amber":
            amber_api_key = env_or_default("AMBER_API_KEY", DEFAULT_AMBER_API_KEY)
            if not amber_api_key:
                raise PlannerUnavailableError("AMBER_API_KEY is not configured")
            amber_client = AmberClient(api_key=amber_api_key)
            prices = amber_client.get_price_horizon(site_id=args.amber_site_id, horizon_hours=args.planner_horizon_hours)
            if args.amber_forecast_log:
                log_amber_forecast(args.amber_forecast_log, prices)
            source = "amber_price_plan"
        else:
            prices = build_manual_price_horizon(horizon_hours=args.planner_horizon_hours, general_per_kwh=args.general_per_kwh)
            source = "manual_price_plan"

        current_interval = prices[0]
        desired_charge_power = desired_charge_watts(
            current_interval=current_interval,
            battery_soc=battery.soc,
            charge_target_soc=args.charge_target_soc,
            max_charge_watts=args.charge_watts,
            base_max_charge_price_c_per_kwh=args.max_charge_price_c_per_kwh,
        )
        derived_action = "charge" if desired_charge_power > 0 else "fallback"
        final_action = derived_action
        discharge_rule_matched, normalized_feed_in_credit, cheap_charge_minutes = should_discharge_now(
            args=args,
            battery_soc=battery.soc,
            prices=prices,
            current_interval=current_interval,
            now=now,
        )

        if discharge_rule_matched:
            final_action = "discharge"

        if final_action == "charge" and not charge_slot_allowed(now):
            final_action = "fallback"
        command_charge_watts = desired_charge_power if final_action == "charge" else args.charge_watts

        log_json(
            "decision",
            {
                "source": source,
                "battery_soc": battery.soc,
                "forecast_general_per_kwh": current_interval.general_per_kwh,
                "forecast_feed_in_per_kwh": current_interval.feed_in_per_kwh,
                "normalized_feed_in_credit_c_per_kwh": normalized_feed_in_credit,
                "command_charge_watts": command_charge_watts,
                "discharge_rule_matched": discharge_rule_matched,
                "cheap_charge_minutes_in_lookahead": cheap_charge_minutes,
                "derived_action": derived_action,
                "final_action": final_action,
            },
        )
        log_json(
            "plan_preview",
            {
                "source": source,
                "current_action": final_action,
                "feasible_plan_through": current_interval.end_time.isoformat(),
                "next_charge_at": current_interval.start_time.isoformat() if derived_action == "charge" else None,
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
        charge_watts=command_charge_watts if action == "charge" else args.charge_watts,
        discharge_watts=args.discharge_watts,
        apply=args.apply,
        log_event=log_json,
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
    parser.add_argument("--lateness-penalty-start-hour", type=int, default=DEFAULT_LATENESS_PENALTY_START_HOUR)
    parser.add_argument("--max-lateness-penalty-c-per-kwh", type=float, default=DEFAULT_MAX_LATENESS_PENALTY_C_PER_KWH)
    parser.add_argument("--forecast-risk-horizon-hours", type=int, default=DEFAULT_FORECAST_RISK_HORIZON_HOURS)
    parser.add_argument("--max-forecast-risk-penalty-c-per-kwh", type=float, default=DEFAULT_MAX_FORECAST_RISK_PENALTY_C_PER_KWH)
    parser.add_argument("--max-charge-price-c-per-kwh", type=float, default=DEFAULT_MAX_CHARGE_PRICE_C_PER_KWH)
    parser.add_argument("--discharge-min-soc", type=int, default=DEFAULT_DISCHARGE_MIN_SOC)
    parser.add_argument(
        "--discharge-feed-in-threshold-c-per-kwh",
        type=float,
        default=DEFAULT_DISCHARGE_FEED_IN_THRESHOLD_C_PER_KWH,
    )
    parser.add_argument("--discharge-cheap-lookahead-hours", type=int, default=DEFAULT_DISCHARGE_CHEAP_LOOKAHEAD_HOURS)
    parser.add_argument(
        "--discharge-cheap-price-threshold-c-per-kwh",
        type=float,
        default=DEFAULT_DISCHARGE_CHEAP_PRICE_THRESHOLD_C_PER_KWH,
    )
    parser.add_argument("--discharge-required-cheap-hours", type=float, default=DEFAULT_DISCHARGE_REQUIRED_CHEAP_HOURS)
    parser.add_argument("--amber-site-id", default=env_or_default("AMBER_SITE_ID", DEFAULT_AMBER_SITE_ID))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--loop-seconds", type=int, default=DEFAULT_LOOP_SECONDS)
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE)
    parser.add_argument("--amber-forecast-log", default=env_or_default("AMBER_FORECAST_LOG", DEFAULT_AMBER_FORECAST_LOG))
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
