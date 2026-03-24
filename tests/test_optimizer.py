import unittest
from datetime import datetime

from amber import AmberPriceSnapshot
from planner import (
    BatterySnapshot,
    ControlPlanStep,
    build_hourly_plan_preview,
    build_price_only_charge_plan,
    compute_forecast_uncertainty_penalty_c_per_kwh,
    compute_lateness_penalty_c_per_kwh,
)
from solplanet import charge_slot_allowed


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def price(start: str, end: str, cents: float, demand_window: bool = False) -> AmberPriceSnapshot:
    start_dt = dt(start)
    end_dt = dt(end)
    return AmberPriceSnapshot(
        site_id="site",
        start_time=start_dt,
        end_time=end_dt,
        duration_minutes=int((end_dt - start_dt).total_seconds() // 60),
        general_per_kwh=cents,
        general_descriptor="test",
        feed_in_per_kwh=None,
        feed_in_descriptor=None,
        demand_window=demand_window,
    )


class MinimalPlannerTests(unittest.TestCase):
    def test_forecast_uncertainty_penalty_ramps_with_time_ahead(self) -> None:
        now = dt("2026-03-22T10:00:00+11:00")
        self.assertEqual(
            compute_forecast_uncertainty_penalty_c_per_kwh(now, now, 6, 1.0),
            0.0,
        )
        self.assertAlmostEqual(
            compute_forecast_uncertainty_penalty_c_per_kwh(dt("2026-03-22T13:00:00+11:00"), now, 6, 1.0),
            0.5,
            places=3,
        )
        self.assertAlmostEqual(
            compute_forecast_uncertainty_penalty_c_per_kwh(dt("2026-03-22T16:00:00+11:00"), now, 6, 1.0),
            1.0,
            places=3,
        )

    def test_lateness_penalty_is_zero_before_start_and_ramps_to_demand_window(self) -> None:
        self.assertEqual(
            compute_lateness_penalty_c_per_kwh(dt("2026-03-22T12:59:00+11:00"), 13, 1.5),
            0.0,
        )
        self.assertAlmostEqual(
            compute_lateness_penalty_c_per_kwh(dt("2026-03-22T14:00:00+11:00"), 13, 1.5),
            0.75,
            places=3,
        )
        self.assertAlmostEqual(
            compute_lateness_penalty_c_per_kwh(dt("2026-03-22T14:30:00+11:00"), 13, 1.5),
            1.125,
            places=3,
        )

    def test_uses_actual_30_min_interval_and_partial_last_interval(self) -> None:
        battery = BatterySnapshot(soc=50, battery_power_watts=0, battery_voltage_raw=0, battery_current_raw=0)
        prices = [
            price("2026-03-21T21:00:00+11:00", "2026-03-21T21:30:00+11:00", 20.0),
            price("2026-03-21T21:30:00+11:00", "2026-03-21T22:00:00+11:00", 5.0),
        ]

        plan = build_price_only_charge_plan(
            battery=battery,
            prices=prices,
            battery_capacity_kwh=10.0,
            charge_target_soc=75,
            planner_charge_kwh_per_minute=10.0 / 60.0,
            charge_watts=15000,
            now=dt("2026-03-21T21:00:00+11:00"),
        )

        self.assertEqual(plan.action, "fallback")
        self.assertAlmostEqual(plan.required_energy_kwh, 2.5, places=3)
        self.assertAlmostEqual(plan.required_charge_minutes, 15.0, places=3)
        self.assertAlmostEqual(plan.planned_charge_kwh, 2.5, places=3)
        self.assertAlmostEqual(plan.planned_charge_minutes, 15.0, places=3)
        self.assertAlmostEqual(plan.estimated_total_charge_cost, 0.12, places=2)
        self.assertAlmostEqual(plan.average_planned_charge_price_c_per_kwh, 5.0, places=3)
        self.assertEqual(plan.selected_minute_count, 16)
        self.assertEqual(plan.next_charge_at, dt("2026-03-21T21:30:00+11:00"))

    def test_mixed_5_and_30_min_intervals_are_handled_consistently(self) -> None:
        battery = BatterySnapshot(soc=50, battery_power_watts=0, battery_voltage_raw=0, battery_current_raw=0)
        prices = [
            price("2026-03-21T22:00:00+11:00", "2026-03-21T22:05:00+11:00", 25.0),
            price("2026-03-21T22:05:00+11:00", "2026-03-21T22:10:00+11:00", 25.0),
            price("2026-03-22T12:00:00+11:00", "2026-03-22T12:30:00+11:00", 4.0),
        ]

        plan = build_price_only_charge_plan(
            battery=battery,
            prices=prices,
            battery_capacity_kwh=10.0,
            charge_target_soc=75,
            planner_charge_kwh_per_minute=10.0 / 60.0,
            charge_watts=15000,
            now=dt("2026-03-21T22:00:00+11:00"),
        )

        self.assertEqual(plan.action, "fallback")
        self.assertAlmostEqual(plan.required_energy_kwh, 2.5, places=3)
        self.assertAlmostEqual(plan.planned_charge_kwh, 2.5, places=3)
        self.assertAlmostEqual(plan.planned_charge_minutes, 15.0, places=3)
        self.assertAlmostEqual(plan.estimated_total_charge_cost, 0.10, places=2)
        self.assertEqual(plan.selected_minute_count, 16)

    def test_current_interval_charges_when_it_is_cheapest(self) -> None:
        battery = BatterySnapshot(soc=50, battery_power_watts=0, battery_voltage_raw=0, battery_current_raw=0)
        prices = [
            price("2026-03-21T22:00:00+11:00", "2026-03-21T22:30:00+11:00", 4.0),
            price("2026-03-21T22:30:00+11:00", "2026-03-21T23:00:00+11:00", 12.0),
        ]

        plan = build_price_only_charge_plan(
            battery=battery,
            prices=prices,
            battery_capacity_kwh=10.0,
            charge_target_soc=75,
            planner_charge_kwh_per_minute=10.0 / 60.0,
            charge_watts=15000,
            now=dt("2026-03-21T22:00:00+11:00"),
        )

        self.assertEqual(plan.action, "charge")

    def test_current_action_uses_selected_current_minute_not_whole_interval(self) -> None:
        battery = BatterySnapshot(soc=50, battery_power_watts=0, battery_voltage_raw=0, battery_current_raw=0)
        prices = [
            price("2026-03-21T22:00:00+11:00", "2026-03-21T22:05:00+11:00", 4.0),
            price("2026-03-21T22:05:00+11:00", "2026-03-21T22:10:00+11:00", 12.0),
        ]

        plan = build_price_only_charge_plan(
            battery=battery,
            prices=prices,
            battery_capacity_kwh=10.0,
            charge_target_soc=52,
            planner_charge_kwh_per_minute=10.0 / 60.0,
            charge_watts=15000,
            now=dt("2026-03-21T22:03:00+11:00"),
        )

        self.assertEqual(plan.action, "charge")
        self.assertAlmostEqual(plan.planned_charge_minutes, 1.2, places=3)
        self.assertEqual(plan.selected_minute_count, 2)
        self.assertEqual(plan.next_charge_at, dt("2026-03-21T22:03:00+11:00"))

    def test_next_charge_at_uses_earliest_selected_time_not_cheapest_bucket_time(self) -> None:
        battery = BatterySnapshot(soc=20, battery_power_watts=0, battery_voltage_raw=0, battery_current_raw=0)
        prices = [
            price("2026-03-22T11:00:00+11:00", "2026-03-22T12:00:00+11:00", 6.0),
            price("2026-03-22T13:30:00+11:00", "2026-03-22T14:30:00+11:00", 4.0),
        ]

        plan = build_price_only_charge_plan(
            battery=battery,
            prices=prices,
            battery_capacity_kwh=10.0,
            charge_target_soc=55,
            planner_charge_kwh_per_minute=0.05,
            charge_watts=15000,
            now=dt("2026-03-22T10:00:00+11:00"),
        )

        self.assertEqual(plan.next_charge_at, dt("2026-03-22T11:00:00+11:00"))

    def test_lateness_penalty_prefers_earlier_minutes_when_raw_prices_are_close(self) -> None:
        battery = BatterySnapshot(soc=50, battery_power_watts=0, battery_voltage_raw=0, battery_current_raw=0)
        prices = [
            price("2026-03-22T13:00:00+11:00", "2026-03-22T13:10:00+11:00", 5.3),
            price("2026-03-22T14:30:00+11:00", "2026-03-22T14:40:00+11:00", 5.0),
        ]

        plan = build_price_only_charge_plan(
            battery=battery,
            prices=prices,
            battery_capacity_kwh=10.0,
            charge_target_soc=58,
            planner_charge_kwh_per_minute=0.05,
            charge_watts=15000,
            lateness_penalty_start_hour=13,
            max_lateness_penalty_c_per_kwh=1.5,
            now=dt("2026-03-22T10:00:00+11:00"),
        )

        self.assertEqual(plan.next_charge_at, dt("2026-03-22T13:00:00+11:00"))
        self.assertGreater(plan.average_planned_effective_price_c_per_kwh, plan.average_planned_charge_price_c_per_kwh)

    def test_forecast_risk_penalty_prefers_nearer_minutes_when_raw_prices_are_close(self) -> None:
        battery = BatterySnapshot(soc=50, battery_power_watts=0, battery_voltage_raw=0, battery_current_raw=0)
        prices = [
            price("2026-03-22T10:30:00+11:00", "2026-03-22T10:40:00+11:00", 5.5),
            price("2026-03-22T14:30:00+11:00", "2026-03-22T14:40:00+11:00", 5.0),
        ]

        plan = build_price_only_charge_plan(
            battery=battery,
            prices=prices,
            battery_capacity_kwh=10.0,
            charge_target_soc=58,
            planner_charge_kwh_per_minute=0.05,
            charge_watts=15000,
            lateness_penalty_start_hour=15,
            max_lateness_penalty_c_per_kwh=0.0,
            forecast_risk_horizon_hours=6,
            max_forecast_risk_penalty_c_per_kwh=1.0,
            now=dt("2026-03-22T10:00:00+11:00"),
        )

        self.assertEqual(plan.next_charge_at, dt("2026-03-22T10:30:00+11:00"))
        self.assertGreater(plan.average_planned_effective_price_c_per_kwh, plan.average_planned_charge_price_c_per_kwh)

    def test_combined_penalties_can_pull_selection_forward(self) -> None:
        battery = BatterySnapshot(soc=40, battery_power_watts=0, battery_voltage_raw=0, battery_current_raw=0)
        prices = [
            price("2026-03-22T11:30:00+11:00", "2026-03-22T11:40:00+11:00", 6.4),
            price("2026-03-22T14:20:00+11:00", "2026-03-22T14:30:00+11:00", 5.0),
        ]

        plan = build_price_only_charge_plan(
            battery=battery,
            prices=prices,
            battery_capacity_kwh=10.0,
            charge_target_soc=58,
            planner_charge_kwh_per_minute=0.05,
            charge_watts=15000,
            lateness_penalty_start_hour=13,
            max_lateness_penalty_c_per_kwh=1.5,
            forecast_risk_horizon_hours=6,
            max_forecast_risk_penalty_c_per_kwh=1.0,
            now=dt("2026-03-22T10:00:00+11:00"),
        )

        self.assertEqual(plan.next_charge_at, dt("2026-03-22T11:30:00+11:00"))

    def test_excludes_demand_window_intervals(self) -> None:
        battery = BatterySnapshot(soc=50, battery_power_watts=0, battery_voltage_raw=0, battery_current_raw=0)
        prices = [
            price("2026-03-21T15:00:00+11:00", "2026-03-21T15:30:00+11:00", 1.0, demand_window=True),
            price("2026-03-21T15:30:00+11:00", "2026-03-21T16:00:00+11:00", 12.0),
        ]

        plan = build_price_only_charge_plan(
            battery=battery,
            prices=prices,
            battery_capacity_kwh=10.0,
            charge_target_soc=75,
            planner_charge_kwh_per_minute=10.0 / 60.0,
            charge_watts=15000,
            now=dt("2026-03-21T15:00:00+11:00"),
        )

        self.assertEqual(plan.steps[0].action, "fallback")
        self.assertEqual(plan.steps[1].action, "charge")

    def test_hourly_plan_preview_marks_charge_hours(self) -> None:
        steps = [
            ControlPlanStep(
                interval_start=dt("2026-03-21T22:00:00+11:00"),
                interval_end=dt("2026-03-21T22:05:00+11:00"),
                action="fallback",
                planned_charge_kwh=0.0,
                planned_charge_minutes=0.0,
                expected_grid_kw=0.0,
                expected_cost=0.0,
                projected_soc=50,
            ),
            ControlPlanStep(
                interval_start=dt("2026-03-21T23:00:00+11:00"),
                interval_end=dt("2026-03-21T23:30:00+11:00"),
                action="charge",
                planned_charge_kwh=2.5,
                planned_charge_minutes=15.0,
                expected_grid_kw=15.0,
                expected_cost=0.1,
                projected_soc=75,
            ),
        ]

        start, hourly_actions, next_charge_at = build_hourly_plan_preview(
            steps,
            (dt("2026-03-21T23:00:00+11:00"),),
            dt("2026-03-21T23:00:00+11:00"),
            hours=4,
        )

        self.assertEqual(start, "2026-03-21T22:00:00+11:00")
        self.assertEqual(hourly_actions, [0, 1, 0, 0])
        self.assertEqual(next_charge_at, "2026-03-21T23:00:00+11:00")

    def test_hourly_plan_preview_uses_selected_minutes_not_parent_interval_start(self) -> None:
        steps = [
            ControlPlanStep(
                interval_start=dt("2026-03-22T12:00:01+11:00"),
                interval_end=dt("2026-03-22T12:30:00+11:00"),
                action="charge",
                planned_charge_kwh=0.1693,
                planned_charge_minutes=1.0,
                expected_grid_kw=15.0,
                expected_cost=0.01,
                projected_soc=40,
            ),
            ControlPlanStep(
                interval_start=dt("2026-03-22T13:00:01+11:00"),
                interval_end=dt("2026-03-22T13:30:00+11:00"),
                action="charge",
                planned_charge_kwh=1.0,
                planned_charge_minutes=6.0,
                expected_grid_kw=15.0,
                expected_cost=0.05,
                projected_soc=42,
            ),
        ]

        start, hourly_actions, next_charge_at = build_hourly_plan_preview(
            steps,
            (dt("2026-03-22T13:00:00+11:00"), dt("2026-03-22T13:01:00+11:00")),
            dt("2026-03-22T13:00:00+11:00"),
            hours=3,
        )

        self.assertEqual(start, "2026-03-22T12:00:00+11:00")
        self.assertEqual(hourly_actions, [0, 1, 0])
        self.assertEqual(next_charge_at, "2026-03-22T13:00:00+11:00")

    def test_charge_slot_allowed_respects_3pm_to_9pm_window(self) -> None:
        self.assertFalse(charge_slot_allowed(dt("2026-03-21T20:45:00+11:00")))
        self.assertTrue(charge_slot_allowed(dt("2026-03-21T21:42:00+11:00")))


if __name__ == "__main__":
    unittest.main()
