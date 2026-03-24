#!/usr/bin/env python3
"""Amber forecast-driven Solplanet battery charger."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from amber import AmberClient, build_manual_price_horizon
from planner import PlannerUnavailableError, build_hourly_plan_preview, build_price_only_charge_plan
from solplanet import InverterUnavailableError, InverterWriteError, SolplanetClient, apply_state, charge_slot_allowed, load_battery_snapshot


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = SCRIPT_DIR / ".env"
DEFAULT_HOST = "192.168.68.119"
DEFAULT_BATTERY_SN = ""
DEFAULT_CHARGE_WATTS = 15000
DEFAULT_PLANNER_CHARGE_KWH_PER_MINUTE = 10.158 / 60.0
DEFAULT_DISCHARGE_WATTS = 1500
DEFAULT_CHARGE_TARGET_SOC = 95
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


def run_once(args: argparse.Namespace) -> None:
    client = SolplanetClient(host=args.host)
    battery = load_battery_snapshot(client, args.battery_sn)
    log_json(
        "battery_state",
        {
            "battery_soc": battery.soc,
            "battery_power_watts": battery.battery_power_watts,
            "battery_voltage_raw": battery.battery_voltage_raw,
            "battery_current_raw": battery.battery_current_raw,
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

        plan = build_price_only_charge_plan(
            battery=battery,
            prices=prices,
            battery_capacity_kwh=args.battery_capacity_kwh,
            charge_target_soc=args.charge_target_soc,
            planner_charge_kwh_per_minute=args.planner_charge_kwh_per_minute,
            charge_watts=args.charge_watts,
            lateness_penalty_start_hour=args.lateness_penalty_start_hour,
            max_lateness_penalty_c_per_kwh=args.max_lateness_penalty_c_per_kwh,
            forecast_risk_horizon_hours=args.forecast_risk_horizon_hours,
            max_forecast_risk_penalty_c_per_kwh=args.max_forecast_risk_penalty_c_per_kwh,
            now=now,
        )
        current_interval = prices[0]
        derived_action = plan.action
        final_action = derived_action
        if derived_action == "charge" and not charge_slot_allowed(now):
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
                "average_planned_effective_price_c_per_kwh": plan.average_planned_effective_price_c_per_kwh,
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
