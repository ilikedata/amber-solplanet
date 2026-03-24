import unittest
from argparse import Namespace
from datetime import datetime
from unittest.mock import patch

import solplanet_price_controller as controller
from amber import AmberPriceSnapshot
from planner import BatterySnapshot, ControlPlan, ControlPlanStep, PlannerUnavailableError


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def make_args(price_source: str = "manual") -> Namespace:
    return Namespace(
        price_source=price_source,
        general_per_kwh=12.5,
        host="host",
        battery_sn="BATTERY123",
        charge_watts=15000,
        planner_charge_kwh_per_minute=10.0 / 60.0,
        discharge_watts=1500,
        charge_target_soc=97,
        battery_capacity_kwh=50.0,
        planner_horizon_hours=24,
        lateness_penalty_start_hour=13,
        max_lateness_penalty_c_per_kwh=1.5,
        forecast_risk_horizon_hours=6,
        max_forecast_risk_penalty_c_per_kwh=1.0,
        amber_site_id="site",
        apply=False,
        loop=False,
        loop_seconds=60,
        log_file="test.ndjson",
        amber_forecast_log=None,
    )


def price(start: str, end: str, cents: float, feed_in: float | None = None) -> AmberPriceSnapshot:
    start_dt = dt(start)
    end_dt = dt(end)
    return AmberPriceSnapshot(
        site_id="site",
        start_time=start_dt,
        end_time=end_dt,
        duration_minutes=int((end_dt - start_dt).total_seconds() // 60),
        general_per_kwh=cents,
        general_descriptor="test",
        feed_in_per_kwh=feed_in,
        feed_in_descriptor="test" if feed_in is not None else None,
        demand_window=False,
    )


def plan(action: str = "fallback") -> ControlPlan:
    steps = [
        ControlPlanStep(
            interval_start=dt("2026-03-21T22:00:00+11:00"),
            interval_end=dt("2026-03-21T22:05:00+11:00"),
            action=action,
            planned_charge_kwh=0.0 if action == "fallback" else 1.0,
            planned_charge_minutes=0.0 if action == "fallback" else 6.0,
            expected_grid_kw=0.0 if action == "fallback" else 15.0,
            expected_cost=0.0 if action == "fallback" else 0.1,
            projected_soc=39 if action == "fallback" else 41,
        ),
        ControlPlanStep(
            interval_start=dt("2026-03-21T22:05:00+11:00"),
            interval_end=dt("2026-03-21T22:10:00+11:00"),
            action="fallback",
            planned_charge_kwh=0.0,
            planned_charge_minutes=0.0,
            expected_grid_kw=0.0,
            expected_cost=0.0,
            projected_soc=39,
        ),
    ]
    return ControlPlan(
        action=action,
        steps=steps,
        selected_minute_starts=(dt("2026-03-21T22:00:00+11:00"),) if action == "charge" else (),
        required_energy_kwh=29.0,
        required_charge_minutes=171.0,
        planned_charge_kwh=29.0,
        planned_charge_minutes=171.0,
        estimated_total_charge_cost=1.4,
        average_planned_charge_price_c_per_kwh=4.836,
        average_planned_effective_price_c_per_kwh=5.1,
        selected_minute_count=172,
        target_reachable=True,
        next_charge_at=dt("2026-03-22T13:00:00+11:00"),
    )


class RunOnceTests(unittest.TestCase):
    def test_run_once_manual_logs_decision_and_plan_preview(self) -> None:
        args = make_args("manual")
        events: list[tuple[str, dict]] = []
        battery = BatterySnapshot(soc=39, battery_power_watts=785, battery_voltage_raw=5200, battery_current_raw=151)
        prices = [price("2026-03-21T22:00:00+11:00", "2026-03-21T22:05:00+11:00", 15.0, -8.0)]

        with patch.object(controller, "log_json", side_effect=lambda label, payload: events.append((label, payload))):
            with patch.object(controller, "SolplanetClient", return_value=object()):
                with patch.object(controller, "load_battery_snapshot", return_value=battery):
                    with patch.object(controller, "build_manual_price_horizon", return_value=prices):
                        with patch.object(controller, "build_price_only_charge_plan", return_value=plan("fallback")):
                            with patch.object(controller, "build_hourly_plan_preview", return_value=("2026-03-21T22:00:00+11:00", [0] * 24, "2026-03-22T13:00:00+11:00")):
                                with patch.object(controller, "apply_state") as apply_state:
                                    controller.run_once(args)

        self.assertEqual([label for label, _ in events[:3]], ["battery_state", "decision", "plan_preview"])
        self.assertEqual(events[1][1]["final_action"], "fallback")
        self.assertEqual(events[2][1]["selected_minute_count"], 172)
        apply_state.assert_called_once()
        self.assertEqual(apply_state.call_args.kwargs["action"], "fallback")

    def test_run_once_amber_charge_is_guarded_to_fallback(self) -> None:
        args = make_args("amber")
        events: list[tuple[str, dict]] = []
        battery = BatterySnapshot(soc=39, battery_power_watts=785, battery_voltage_raw=5200, battery_current_raw=151)
        prices = [price("2026-03-21T20:45:00+11:00", "2026-03-21T20:50:00+11:00", 5.0, -8.0)]

        with patch.object(controller, "log_json", side_effect=lambda label, payload: events.append((label, payload))):
            with patch.object(controller, "env_or_default", return_value="token"):
                with patch.object(controller, "AmberClient") as amber_client_cls:
                    amber_client_cls.return_value.get_price_horizon.return_value = prices
                    with patch.object(controller, "SolplanetClient", return_value=object()):
                        with patch.object(controller, "load_battery_snapshot", return_value=battery):
                            with patch.object(controller, "build_price_only_charge_plan", return_value=plan("charge")):
                                with patch.object(controller, "charge_slot_allowed", return_value=False):
                                    with patch.object(controller, "build_hourly_plan_preview", return_value=("2026-03-21T20:00:00+11:00", [1] + [0] * 23, "2026-03-21T20:45:00+11:00")):
                                        with patch.object(controller, "apply_state") as apply_state:
                                            controller.run_once(args)

        self.assertEqual(events[1][1]["derived_action"], "charge")
        self.assertEqual(events[1][1]["final_action"], "fallback")
        self.assertEqual(apply_state.call_args.kwargs["action"], "fallback")

    def test_run_once_logs_planner_fallback_when_amber_key_missing(self) -> None:
        args = make_args("amber")
        events: list[tuple[str, dict]] = []
        battery = BatterySnapshot(soc=39, battery_power_watts=785, battery_voltage_raw=5200, battery_current_raw=151)

        with patch.object(controller, "log_json", side_effect=lambda label, payload: events.append((label, payload))):
            with patch.object(controller, "env_or_default", return_value=""):
                with patch.object(controller, "SolplanetClient", return_value=object()):
                    with patch.object(controller, "load_battery_snapshot", return_value=battery):
                        with patch.object(controller, "apply_state") as apply_state:
                            controller.run_once(args)

        self.assertEqual(events[1][0], "planner_fallback")
        self.assertIn("AMBER_API_KEY", events[1][1]["message"])
        self.assertEqual(apply_state.call_args.kwargs["action"], "fallback")

    def test_run_once_logs_planner_fallback_on_plan_error(self) -> None:
        args = make_args("manual")
        events: list[tuple[str, dict]] = []
        battery = BatterySnapshot(soc=39, battery_power_watts=785, battery_voltage_raw=5200, battery_current_raw=151)
        prices = [price("2026-03-21T22:00:00+11:00", "2026-03-21T22:05:00+11:00", 15.0)]

        with patch.object(controller, "log_json", side_effect=lambda label, payload: events.append((label, payload))):
            with patch.object(controller, "SolplanetClient", return_value=object()):
                with patch.object(controller, "load_battery_snapshot", return_value=battery):
                    with patch.object(controller, "build_manual_price_horizon", return_value=prices):
                        with patch.object(controller, "build_price_only_charge_plan", side_effect=PlannerUnavailableError("boom")):
                            with patch.object(controller, "apply_state") as apply_state:
                                controller.run_once(args)

        self.assertEqual(events[1][0], "planner_fallback")
        self.assertEqual(events[1][1]["fallback_action"], "fallback")
        self.assertEqual(apply_state.call_args.kwargs["action"], "fallback")

    def test_run_once_amber_logs_forecast_when_enabled(self) -> None:
        args = make_args("amber")
        args.amber_forecast_log = "test_forecast.ndjson"
        events: list[tuple[str, dict]] = []
        battery = BatterySnapshot(soc=39, battery_power_watts=785, battery_voltage_raw=5200, battery_current_raw=151)
        prices = [price("2026-03-21T20:45:00+11:00", "2026-03-21T20:50:00+11:00", 5.0, -8.0)]

        with patch.object(controller, "log_json", side_effect=lambda label, payload: events.append((label, payload))):
            with patch.object(controller, "log_amber_forecast") as log_forecast:
                with patch.object(controller, "env_or_default", return_value="token"):
                    with patch.object(controller, "AmberClient") as amber_client_cls:
                        amber_client_cls.return_value.get_price_horizon.return_value = prices
                        with patch.object(controller, "SolplanetClient", return_value=object()):
                            with patch.object(controller, "load_battery_snapshot", return_value=battery):
                                with patch.object(controller, "build_price_only_charge_plan", return_value=plan("fallback")):
                                    with patch.object(controller, "build_hourly_plan_preview", return_value=("2026-03-21T20:00:00+11:00", [0] * 24, "2026-03-22T13:00:00+11:00")):
                                        with patch.object(controller, "apply_state"):
                                            controller.run_once(args)

        log_forecast.assert_called_once_with("test_forecast.ndjson", prices)


if __name__ == "__main__":
    unittest.main()
