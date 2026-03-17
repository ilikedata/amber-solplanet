#!/usr/bin/env python3
"""Drive Solplanet battery mode from a coarse electricity price state.

This script implements a minimal state machine for local control:
- `high` falls back to self-consumption mode.
- `low` enables a 30-minute discharge override in custom mode.
- `extremely_low` enables a 30-minute charge override in custom mode.

Assumptions:
- A short 30-minute slot is safer than long-lived schedules because stale
  overrides expire quickly if automation breaks.
- `Pin` and `Pout` are the effective schedule power setpoints in watts.
- The safe fallback is self-consumption mode with an empty schedule.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime
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
DEFAULT_DISCHARGE_WATTS = 1500
DEFAULT_CHARGE_TARGET_SOC = 97
DEFAULT_DISCHARGE_TARGET_SOC = 40
DEFAULT_PRICE_SOURCE = "manual"
DEFAULT_AMBER_SITE_ID = ""
DEFAULT_AMBER_API_KEY = ""
DEFAULT_LOOP_SECONDS = 300
DEFAULT_REQUEST_RETRIES = 2
DEFAULT_RETRY_DELAY_SECONDS = 2.0
DEFAULT_LOG_FILE = "solplanet_price_controller.ndjson"
DEFAULT_MIN_DISCHARGE_FEED_IN_PRICE = -30.0
DEMAND_WINDOW_START_HOUR = 15
BATTERY_DEVICE = 4
ACTION_SET_BATTERY = "setbattery"
ACTION_SET_DEFINE = "setdefine"
SELF_CONSUMPTION_MODE = 2
CUSTOM_MODE = 4
DAYS = ["Mon", "Tus", "Wen", "Thu", "Fri", "Sat", "Sun"]
AMBER_DESCRIPTORS = [
    "negative",
    "extremelyLow",
    "veryLow",
    "low",
    "neutral",
    "high",
    "spike",
]


@dataclass(frozen=True)
class Slot:
    day: str
    start_hour: int
    start_minute: int
    duration_hours: int
    mode: str

    def to_raw(self) -> int:
        """Encode a schedule slot in the inverter's native format."""
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


class InverterUnavailableError(RuntimeError):
    """Raised when the inverter cannot be reached safely."""


class InverterWriteError(RuntimeError):
    """Raised when a write to the inverter fails."""


LOG_FILE_PATH: Path | None = None


def load_dotenv(dotenv_path: Path = DEFAULT_ENV_FILE) -> None:
    """Load a simple KEY=VALUE .env file into the process environment."""
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


def env_or_default(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def configure_logging(log_file: str) -> None:
    """Configure append-only NDJSON logging."""
    global LOG_FILE_PATH
    log_path = Path(log_file).expanduser()
    if not log_path.is_absolute():
        log_path = Path.cwd() / log_path
    LOG_FILE_PATH = log_path


def log_json(label: str, payload: dict[str, Any]) -> None:
    """Write one NDJSON record and mirror it to stdout."""
    record = {
        "ts": datetime.now().astimezone().isoformat(),
        "event": label,
        **payload,
    }
    line = json.dumps(record, sort_keys=True)
    if LOG_FILE_PATH is not None:
        with LOG_FILE_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    print(line)


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
        data = json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}/{endpoint}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._send(request)

    def _send(self, request: Request) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self.request_retries + 1):
            try:
                with urlopen(
                    request,
                    timeout=self.timeout_seconds,
                    context=self.ssl_context,
                ) as response:
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


@dataclass(frozen=True)
class AmberPriceSnapshot:
    site_id: str
    nem_time: str
    per_kwh: float
    descriptor: str
    interval_type: str
    channel_type: str
    demand_window: bool | None


@dataclass(frozen=True)
class AmberDecisionSnapshot:
    general: AmberPriceSnapshot
    feed_in: AmberPriceSnapshot | None
    action: str


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

    def get_current_prices_by_channel(
        self,
        site_id: str,
        resolution: int = 5,
    ) -> dict[str, AmberPriceSnapshot]:
        last_exc: Exception | None = None
        for attempt in range(self.request_retries + 1):
            try:
                configuration = Configuration(access_token=self.api_key)
                with ApiClient(configuration) as api_client:
                    api = AmberApi(api_client)
                    intervals = api.get_current_prices(
                        site_id,
                        previous=0,
                        next=0,
                        resolution=resolution,
                    )
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < self.request_retries:
                    time.sleep(self.retry_delay_seconds)
                else:
                    raise RuntimeError(f"Amber API error: {exc}") from exc
        if not intervals:
            raise RuntimeError("Amber returned no current price intervals")
        snapshots: dict[str, AmberPriceSnapshot] = {}
        for interval in intervals:
            actual = interval.actual_instance
            channel_type = str(actual.channel_type.value)
            snapshots[channel_type] = AmberPriceSnapshot(
                site_id=site_id,
                nem_time=actual.nem_time.isoformat(),
                per_kwh=float(actual.per_kwh),
                descriptor=str(actual.descriptor.value),
                interval_type=str(actual.type),
                channel_type=channel_type,
                demand_window=None
                if actual.tariff_information is None
                else bool(actual.tariff_information.demand_window),
            )
        return snapshots


def current_slot(now: datetime | None = None) -> Slot:
    """Return a backdated 1-hour slot so remaining runtime is usually <= 30 minutes.

    The slot start is shifted back by one half-hour relative to the current half-hour
    bucket. The only exception is the first half-hour after midnight, where we clamp
    to `00:00-01:00` rather than attempting to write a previous-day slot.
    """
    now = now or datetime.now().astimezone()
    if now.hour == 0 and now.minute < 30:
        return Slot(
            day=DAYS[now.weekday()],
            start_hour=0,
            start_minute=0,
            duration_hours=1,
            mode="charge",
        )

    day = DAYS[now.weekday()]
    if now.minute < 30:
        return Slot(
            day=day,
            start_hour=now.hour - 1,
            start_minute=30,
            duration_hours=1,
            mode="charge",
        )
    return Slot(
        day=day,
        start_hour=now.hour,
        start_minute=0,
        duration_hours=1,
        mode="charge",
    )


def empty_schedule(pin: int = 0, pout: int = 0) -> dict[str, Any]:
    schedule: dict[str, Any] = {"Pin": pin, "Pout": pout}
    for day in DAYS:
        schedule[day] = [0, 0, 0, 0, 0, 0]
    return schedule


def active_schedule(mode: str, watts: int, now: datetime | None = None) -> dict[str, Any]:
    slot = current_slot(now)
    slot = Slot(
        day=slot.day,
        start_hour=slot.start_hour,
        start_minute=slot.start_minute,
        duration_hours=1,
        mode=mode,
    )
    schedule = empty_schedule(
        pin=watts if mode == "charge" else 0,
        pout=watts if mode == "discharge" else 0,
    )
    schedule[slot.day][0] = slot.to_raw()
    return schedule


def charge_slot_allowed(now: datetime | None = None) -> bool:
    """Return whether a backdated charge slot can stay fully before 3pm local time."""
    now = now or datetime.now().astimezone()
    slot = current_slot(now)
    end_hour = slot.start_hour + 1
    return end_hour <= DEMAND_WINDOW_START_HOUR


def build_setbattery_payload(
    battery_info: dict[str, Any],
    battery_sn: str,
    mode_register: int,
) -> dict[str, Any]:
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


def desired_state(
    action: str,
    charge_watts: int,
    discharge_watts: int,
) -> tuple[int, dict[str, Any], str]:
    if action == "fallback":
        return SELF_CONSUMPTION_MODE, empty_schedule(), "fallback to self-consumption"
    if action == "discharge":
        return CUSTOM_MODE, active_schedule("discharge", discharge_watts), "discharge override"
    if action == "charge":
        return CUSTOM_MODE, active_schedule("charge", charge_watts), "charge override"
    raise ValueError(f"Unsupported action: {action}")


def decide_from_amber_prices(
    general: AmberPriceSnapshot,
    feed_in: AmberPriceSnapshot | None,
) -> AmberDecisionSnapshot:
    if general.demand_window:
        return AmberDecisionSnapshot(
            general=general,
            feed_in=feed_in,
            action="fallback",
        )

    if general.descriptor in {"negative", "extremelyLow"}:
        return AmberDecisionSnapshot(
            general=general,
            feed_in=feed_in,
            action="charge",
        )

    if feed_in is not None and (
        feed_in.descriptor == "spike"
        or (feed_in.descriptor == "high" and feed_in.per_kwh <= DEFAULT_MIN_DISCHARGE_FEED_IN_PRICE)
    ):
        return AmberDecisionSnapshot(
            general=general,
            feed_in=feed_in,
            action="discharge",
        )

    return AmberDecisionSnapshot(
        general=general,
        feed_in=feed_in,
        action="fallback",
    )


def apply_soc_gates(
    action: str,
    soc: int,
    charge_target_soc: int,
    discharge_target_soc: int,
) -> str:
    if action == "charge" and soc >= charge_target_soc:
        return "fallback"
    if action == "discharge" and soc <= discharge_target_soc:
        return "fallback"
    return action


def apply_charge_window_guard(action: str, now: datetime | None = None) -> str:
    """Prevent charge schedules from extending into the 3pm demand window."""
    if action != "charge":
        return action
    if not charge_slot_allowed(now):
        return "fallback"
    return action


def apply_state(
    client: SolplanetClient,
    battery_sn: str,
    action: str,
    charge_watts: int,
    discharge_watts: int,
    apply: bool,
) -> None:
    battery_info = client.get_json(f"getdev.cgi?device=4&sn={battery_sn}")
    mode_register, schedule, description = desired_state(
        action=action,
        charge_watts=charge_watts,
        discharge_watts=discharge_watts,
    )
    mode_payload = build_setbattery_payload(
        battery_info=battery_info,
        battery_sn=battery_sn,
        mode_register=mode_register,
    )
    schedule_payload = build_setdefine_payload(schedule)

    if not apply:
        log_json("dry_run", {"action": action, "description": description})
        return

    try:
        # Clear stale schedule first so fallback and overrides are deterministic.
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
            "mode_response": schedule_response.get("dat"),
            "schedule_response": mode_response.get("dat"),
            "confirmed_mod_r": confirmed_mode.get("mod_r"),
            "confirmed_pin": confirmed_schedule.get("Pin"),
            "confirmed_pout": confirmed_schedule.get("Pout"),
        },
    )


def run_once(args: argparse.Namespace) -> None:
    client = SolplanetClient(host=args.host)
    try:
        battery_data = client.get_json(f"getdevdata.cgi?device=4&sn={args.battery_sn}")
        soc = int(battery_data["soc"])
        battery_power_watts = int(battery_data.get("pb", 0))
        battery_voltage_raw = int(battery_data.get("vb", 0))
        battery_current_raw = int(battery_data.get("cb", 0))
    except Exception as exc:  # noqa: BLE001
        raise InverterUnavailableError(f"Failed to read battery telemetry: {exc}") from exc

    log_json(
        "battery_state",
        {
            "battery_soc": soc,
            "battery_power_watts": battery_power_watts,
            "battery_voltage_raw": battery_voltage_raw,
            "battery_current_raw": battery_current_raw,
        },
    )

    action = "fallback"
    amber_error: str | None = None
    if args.price_source == "amber":
        try:
            amber_api_key = env_or_default("AMBER_API_KEY", DEFAULT_AMBER_API_KEY)
            if not amber_api_key:
                raise RuntimeError("AMBER_API_KEY is not configured")
            amber_client = AmberClient(api_key=amber_api_key)
            amber_prices = amber_client.get_current_prices_by_channel(site_id=args.amber_site_id)
            general_price = amber_prices.get("general")
            if general_price is None:
                raise RuntimeError("Amber returned no current general price")
            feed_in_price = amber_prices.get("feedIn")
            amber_decision = decide_from_amber_prices(
                general=general_price,
                feed_in=feed_in_price,
            )
            action = amber_decision.action
            gated_action = apply_soc_gates(
                action=action,
                soc=soc,
                charge_target_soc=args.charge_target_soc,
                discharge_target_soc=args.discharge_target_soc,
            )
            final_action = apply_charge_window_guard(gated_action)
            log_json(
                "decision",
                {
                    "source": "amber",
                    "battery_soc": soc,
                    "general_descriptor": amber_decision.general.descriptor,
                    "general_per_kwh": amber_decision.general.per_kwh,
                    "general_demand_window": amber_decision.general.demand_window,
                    "feed_in_descriptor": None if amber_decision.feed_in is None else amber_decision.feed_in.descriptor,
                    "feed_in_per_kwh": None if amber_decision.feed_in is None else amber_decision.feed_in.per_kwh,
                    "derived_action": action,
                    "final_action": final_action,
                },
            )
            action = final_action
        except Exception as exc:  # noqa: BLE001
            amber_error = str(exc)
            action = "fallback"
            log_json(
                "amber_error",
                {
                    "amber_error": amber_error,
                    "battery_soc": soc,
                    "fallback_action": action,
                },
            )
    else:
        manual_general = AmberPriceSnapshot(
            site_id="manual",
            nem_time="manual",
            per_kwh=0.0,
            descriptor=args.general_descriptor,
            interval_type="manual",
            channel_type="general",
            demand_window=False,
        )
        manual_feed_in = AmberPriceSnapshot(
            site_id="manual",
            nem_time="manual",
            per_kwh=0.0,
            descriptor=args.feed_in_descriptor,
            interval_type="manual",
            channel_type="feedIn",
            demand_window=None,
        )
        manual_decision = decide_from_amber_prices(
            general=manual_general,
            feed_in=manual_feed_in,
        )
        action = apply_soc_gates(
            action=manual_decision.action,
            soc=soc,
            charge_target_soc=args.charge_target_soc,
            discharge_target_soc=args.discharge_target_soc,
        )
        action = apply_charge_window_guard(action)
        log_json(
            "decision",
            {
                "source": "manual",
                "general": args.general_descriptor,
                "feed_in": args.feed_in_descriptor,
                "general_demand_window": False,
                "battery_soc": soc,
                "derived_action": manual_decision.action,
                "final_action": action,
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
    parser = argparse.ArgumentParser(
        description="Minimal Solplanet price-state controller."
    )
    parser.add_argument(
        "--price-source",
        choices=["manual", "amber"],
        default=DEFAULT_PRICE_SOURCE,
        help="Use hardcoded Amber descriptors or derive them from Amber current pricing.",
    )
    parser.add_argument(
        "--general-descriptor",
        choices=AMBER_DESCRIPTORS,
        default="neutral",
        help="Hardcoded Amber general descriptor for testing when --price-source=manual.",
    )
    parser.add_argument(
        "--feed-in-descriptor",
        choices=AMBER_DESCRIPTORS,
        default="neutral",
        help="Hardcoded Amber feed-in descriptor for testing when --price-source=manual.",
    )
    parser.add_argument(
        "--host",
        default=env_or_default("SOLPLANET_HOST", DEFAULT_HOST),
        help="Solplanet inverter host.",
    )
    parser.add_argument(
        "--battery-sn",
        default=env_or_default("SOLPLANET_BATTERY_SN", DEFAULT_BATTERY_SN),
        help="Battery serial number.",
    )
    parser.add_argument(
        "--charge-watts",
        type=int,
        default=DEFAULT_CHARGE_WATTS,
        help="Charge power used for extremely_low.",
    )
    parser.add_argument(
        "--discharge-watts",
        type=int,
        default=DEFAULT_DISCHARGE_WATTS,
        help="Discharge power used for low.",
    )
    parser.add_argument(
        "--charge-target-soc",
        type=int,
        default=DEFAULT_CHARGE_TARGET_SOC,
        help="Stop charging and fall back once battery SOC reaches this percentage.",
    )
    parser.add_argument(
        "--discharge-target-soc",
        type=int,
        default=DEFAULT_DISCHARGE_TARGET_SOC,
        help="Stop discharging and fall back once battery SOC falls to this percentage.",
    )
    parser.add_argument(
        "--amber-site-id",
        default=env_or_default("AMBER_SITE_ID", DEFAULT_AMBER_SITE_ID),
        help="Amber site id for current price fetches.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes to the inverter. Default is dry-run.",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously instead of a single control cycle.",
    )
    parser.add_argument(
        "--loop-seconds",
        type=int,
        default=DEFAULT_LOOP_SECONDS,
        help="Sleep interval between control cycles when --loop is enabled.",
    )
    parser.add_argument(
        "--log-file",
        default=DEFAULT_LOG_FILE,
        help="Append-only log file path.",
    )
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
                log_json(
                    "error",
                    {
                        "cycle_error": "inverter_unavailable",
                        "message": str(exc),
                        "next_action": "retry_next_cycle",
                    },
                )
            except InverterWriteError as exc:
                log_json(
                    "error",
                    {
                        "cycle_error": "inverter_write_failed",
                        "message": str(exc),
                        "next_action": "retry_next_cycle",
                    },
                )
            except Exception as exc:  # noqa: BLE001
                log_json(
                    "error",
                    {
                        "cycle_error": "unexpected",
                        "message": str(exc),
                        "next_action": "retry_next_cycle",
                    },
                )
            time.sleep(args.loop_seconds)
    except KeyboardInterrupt:
        log_json("stopped", {"message": "Stopped by user"})
        return 0
    except Exception as exc:  # noqa: BLE001
        log_json("fatal_error", {"message": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
