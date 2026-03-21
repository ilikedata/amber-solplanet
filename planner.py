from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from amber import AmberPriceSnapshot, floor_to_minute


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


class PlannerUnavailableError(RuntimeError):
    """Raised when planning inputs are unavailable."""


def cents_per_kwh_to_dollars(cents: float) -> float:
    return cents / 100.0


def build_price_only_charge_plan(
    battery: BatterySnapshot,
    prices: list[AmberPriceSnapshot],
    battery_capacity_kwh: float,
    charge_target_soc: int,
    planner_charge_kwh_per_minute: float,
    charge_watts: int,
    now: datetime | None = None,
) -> ControlPlan:
    if not prices:
        raise PlannerUnavailableError("No Amber forecast intervals available")
    if battery_capacity_kwh <= 0:
        raise PlannerUnavailableError("Battery capacity must be positive")
    if planner_charge_kwh_per_minute <= 0:
        raise PlannerUnavailableError("Planner charge kWh per minute must be positive")

    now = floor_to_minute(now or datetime.now().astimezone())
    current_energy_kwh = (battery.soc / 100.0) * battery_capacity_kwh
    target_energy_kwh = (charge_target_soc / 100.0) * battery_capacity_kwh
    required_energy_kwh = max(0.0, target_energy_kwh - current_energy_kwh)
    required_charge_minutes = required_energy_kwh / planner_charge_kwh_per_minute if required_energy_kwh > 0 else 0.0

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
        selected_kwh = min(planner_charge_kwh_per_minute, remaining_energy_kwh)
        if selected_kwh <= 0:
            continue
        selected_energy_by_end[minute_bucket.parent_end_time] = selected_energy_by_end.get(minute_bucket.parent_end_time, 0.0) + selected_kwh
        selected_minutes_by_end[minute_bucket.parent_end_time] = selected_minutes_by_end.get(minute_bucket.parent_end_time, 0.0) + (
            selected_kwh / planner_charge_kwh_per_minute
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
        expected_grid_kw = (charge_watts / 1000.0) if action == "charge" else 0.0
        expected_cost = selected_kwh * cents_per_kwh_to_dollars(price.general_per_kwh)
        projected_energy_kwh = min(target_energy_kwh, projected_energy_kwh + selected_kwh)
        projected_soc = int(round((projected_energy_kwh / battery_capacity_kwh) * 100.0))
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

    return ControlPlan(
        action="charge" if now in selected_minute_buckets else "fallback",
        steps=steps,
        selected_minute_starts=tuple(sorted(selected_minute_buckets)),
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
