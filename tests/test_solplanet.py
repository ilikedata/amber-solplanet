import json
import unittest
from datetime import datetime
from pathlib import Path

from planner import BatterySnapshot
from solplanet import (
    InverterUnavailableError,
    InverterWriteError,
    active_schedule,
    apply_state,
    charge_slot_allowed,
    current_slot,
    desired_state,
    load_battery_snapshot,
    load_battery_snapshot_with_telemetry,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class FakeSolplanetClient:
    def __init__(self) -> None:
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict]] = []
        self.device_info = load_fixture("solplanet_getdev.json")
        self.telemetry = load_fixture("solplanet_getdevdata.json")
        self.schedule = load_fixture("solplanet_getdefine.json")

    def get_json(self, endpoint: str) -> dict:
        self.get_calls.append(endpoint)
        if endpoint.startswith("getdevdata.cgi"):
            return self.telemetry
        if endpoint.startswith("getdev.cgi"):
            return self.device_info
        if endpoint == "getdefine.cgi":
            return self.schedule
        raise AssertionError(f"Unexpected GET endpoint {endpoint}")

    def post_json(self, endpoint: str, payload: dict) -> dict:
        self.post_calls.append((endpoint, payload))
        if payload["action"] == "setdefine":
            return {"dat": "schedule-written"}
        if payload["action"] == "setbattery":
            return {"dat": "mode-written"}
        raise AssertionError(f"Unexpected POST payload {payload}")


class SolplanetBoundaryTests(unittest.TestCase):
    def test_current_slot_uses_previous_half_hour_when_before_boundary(self) -> None:
        slot = current_slot(dt("2026-03-21T22:10:00+11:00"))

        self.assertEqual(slot.start_hour, 21)
        self.assertEqual(slot.start_minute, 30)

    def test_current_slot_uses_current_hour_after_half_hour(self) -> None:
        slot = current_slot(dt("2026-03-21T22:45:00+11:00"))

        self.assertEqual(slot.start_hour, 22)
        self.assertEqual(slot.start_minute, 0)

    def test_charge_slot_allowed_boundaries(self) -> None:
        self.assertTrue(charge_slot_allowed(dt("2026-03-21T14:59:00+11:00")))
        self.assertFalse(charge_slot_allowed(dt("2026-03-21T15:00:00+11:00")))
        self.assertFalse(charge_slot_allowed(dt("2026-03-21T21:00:00+11:00")))
        self.assertTrue(charge_slot_allowed(dt("2026-03-21T21:30:00+11:00")))

    def test_desired_state_builds_charge_and_fallback_modes(self) -> None:
        fallback_mode, fallback_schedule, _ = desired_state("fallback", 15000, 1500)
        charge_mode, charge_schedule, _ = desired_state("charge", 15000, 1500)

        self.assertEqual(fallback_mode, 2)
        self.assertEqual(fallback_schedule["Pin"], 0)
        self.assertEqual(charge_mode, 4)
        self.assertEqual(charge_schedule["Pin"], 15000)

    def test_active_schedule_sets_discharge_power(self) -> None:
        schedule = active_schedule("discharge", 1500, dt("2026-03-21T22:45:00+11:00"))

        self.assertEqual(schedule["Pin"], 0)
        self.assertEqual(schedule["Pout"], 1500)

    def test_load_battery_snapshot_parses_real_shape_payload(self) -> None:
        client = FakeSolplanetClient()

        battery = load_battery_snapshot(client, "BATTERY123")

        self.assertEqual(
            battery,
            BatterySnapshot(soc=39, battery_power_watts=785, battery_voltage_raw=5200, battery_current_raw=151),
        )
        self.assertEqual(client.get_calls, ["getdevdata.cgi?device=4&sn=BATTERY123"])

    def test_load_battery_snapshot_with_telemetry_returns_raw_payload(self) -> None:
        client = FakeSolplanetClient()

        battery, telemetry = load_battery_snapshot_with_telemetry(client, "BATTERY123")

        self.assertEqual(
            battery,
            BatterySnapshot(soc=39, battery_power_watts=785, battery_voltage_raw=5200, battery_current_raw=151),
        )
        self.assertEqual(telemetry, load_fixture("solplanet_getdevdata.json"))
        self.assertEqual(client.get_calls, ["getdevdata.cgi?device=4&sn=BATTERY123"])

    def test_apply_state_writes_expected_payloads_and_logs_confirmation(self) -> None:
        client = FakeSolplanetClient()
        events: list[tuple[str, dict]] = []

        apply_state(
            client=client,
            battery_sn="BATTERY123",
            action="charge",
            charge_watts=15000,
            discharge_watts=1500,
            apply=True,
            log_event=lambda label, payload: events.append((label, payload)),
        )

        self.assertEqual(
            [payload["action"] for _, payload in client.post_calls],
            ["setdefine", "setbattery"],
        )
        self.assertEqual(events[0][0], "applied")
        self.assertEqual(events[0][1]["schedule_response"], "schedule-written")
        self.assertEqual(events[0][1]["mode_response"], "mode-written")
        self.assertEqual(events[0][1]["confirmed_pin"], 15000)
        self.assertEqual(events[0][1]["confirmed_mod_r"], 2)

    def test_apply_state_dry_run_does_not_post(self) -> None:
        client = FakeSolplanetClient()
        events: list[tuple[str, dict]] = []

        apply_state(
            client=client,
            battery_sn="BATTERY123",
            action="fallback",
            charge_watts=15000,
            discharge_watts=1500,
            apply=False,
            log_event=lambda label, payload: events.append((label, payload)),
        )

        self.assertEqual(client.post_calls, [])
        self.assertEqual(events, [("dry_run", {"action": "fallback", "description": "fallback to self-consumption"})])

    def test_apply_state_raises_on_write_failure(self) -> None:
        client = FakeSolplanetClient()

        def failing_post(_endpoint: str, _payload: dict) -> dict:
            raise RuntimeError("write failed")

        client.post_json = failing_post  # type: ignore[method-assign]

        with self.assertRaises(InverterWriteError):
            apply_state(
                client=client,
                battery_sn="BATTERY123",
                action="charge",
                charge_watts=15000,
                discharge_watts=1500,
                apply=True,
                log_event=lambda *_args: None,
            )

    def test_apply_state_raises_on_confirmation_failure(self) -> None:
        client = FakeSolplanetClient()

        def flaky_get(endpoint: str) -> dict:
            if endpoint == "getdefine.cgi":
                raise RuntimeError("confirm failed")
            return FakeSolplanetClient.get_json(client, endpoint)

        client.get_json = flaky_get  # type: ignore[method-assign]

        with self.assertRaises(InverterUnavailableError):
            apply_state(
                client=client,
                battery_sn="BATTERY123",
                action="charge",
                charge_watts=15000,
                discharge_watts=1500,
                apply=True,
                log_event=lambda *_args: None,
            )


if __name__ == "__main__":
    unittest.main()
