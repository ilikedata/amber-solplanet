from __future__ import annotations

import json
import ssl
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from planner import BatterySnapshot


REQUEST_RETRIES = 2
RETRY_DELAY_SECONDS = 2.0
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


class InverterUnavailableError(RuntimeError):
    """Raised when the inverter cannot be reached safely."""


class InverterWriteError(RuntimeError):
    """Raised when a write to the inverter fails."""


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


def desired_state(action: str, charge_watts: int, discharge_watts: int) -> tuple[int, dict[str, Any], str]:
    if action == "fallback":
        return SELF_CONSUMPTION_MODE, empty_schedule(), "fallback to self-consumption"
    if action == "charge":
        return CUSTOM_MODE, active_schedule("charge", charge_watts), "charge override"
    if action == "discharge":
        return CUSTOM_MODE, active_schedule("discharge", discharge_watts), "discharge override"
    raise ValueError(f"Unsupported action: {action}")


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


class SolplanetClient:
    def __init__(
        self,
        host: str,
        timeout_seconds: float = 10.0,
        request_retries: int = REQUEST_RETRIES,
        retry_delay_seconds: float = RETRY_DELAY_SECONDS,
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


def apply_state(
    client: SolplanetClient,
    battery_sn: str,
    action: str,
    charge_watts: int,
    discharge_watts: int,
    apply: bool,
    log_event: Callable[[str, dict[str, Any]], None],
) -> None:
    battery_info = client.get_json(f"getdev.cgi?device=4&sn={battery_sn}")
    mode_register, schedule, description = desired_state(action, charge_watts, discharge_watts)
    mode_payload = build_setbattery_payload(battery_info, battery_sn, mode_register)
    schedule_payload = build_setdefine_payload(schedule)

    if not apply:
        log_event("dry_run", {"action": action, "description": description})
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

    log_event(
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
