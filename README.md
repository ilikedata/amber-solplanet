# Amber Electric Solplanet Inverter/Battery Controller

## Introduction

This repository contains a small local controller for **Solplanet battery/inverter systems** on **Amber Electric** pricing.

It plans **grid charging** from the Amber forecast at the **lowest forecast cost**, while excluding the daily demand window, and can optionally force **battery discharge for export** when the feed-in price is high and cheap non-demand-window recharge time is available later.

## What The Controller Does

On each loop, the controller:

1. reads battery telemetry from the local Solplanet API
2. fetches the Amber forecast horizon
3. calculates required battery energy to reach the target SOC
4. converts the planner charge rate into **kWh per minute**
5. expands the Amber forecast into 1-minute planning buckets
6. selects the cheapest non-demand-window minutes until that energy is covered
7. allows a **partial final minute** for the last bit of required energy
8. decides whether the current minute should `charge`, `discharge`, or `fallback`
9. applies only the current short-lived action using the local Solplanet control API

The fallback action is **self-consumption mode**.

## Control Approach

The planner is intentionally minimal.

It uses:

- Amber forecast prices only
- a 1-minute internal planning grid derived from the Amber forecast
- a soft lateness premium to avoid over-reliance on the final cheap minutes before the demand window
- a soft forecast-horizon premium to avoid over-trusting cheap prices far into the future
- battery capacity
- a normalized planner charge rate in `kWh/min`
- hard demand-window exclusion for charging

It does **not** use:

- household usage history
- weather
- solar forecasting
- learned models

## Configuration

The controller reads local configuration from `.env`:

- `SOLPLANET_HOST`
- `SOLPLANET_BATTERY_SN`
- `AMBER_SITE_ID`
- `AMBER_API_KEY`

See [`.env.example`](/home/tai/svn/electricity/.env.example).

Useful CLI tuning knobs:

- `--battery-capacity-kwh` default `50.0`
- `--planner-horizon-hours` default `24`
- `--charge-watts` default `15000`
- `--planner-charge-kwh-per-minute` default `0.1693`
- `--charge-target-soc` default `95`
- `--lateness-penalty-start-hour` default `13`
- `--max-lateness-penalty-c-per-kwh` default `1.5`
- `--forecast-risk-horizon-hours` default `6`
- `--max-forecast-risk-penalty-c-per-kwh` default `1.0`
- `--discharge-min-soc` default `55`
- `--discharge-feed-in-threshold-c-per-kwh` default `18.0`
- `--discharge-cheap-lookahead-hours` default `24`
- `--discharge-cheap-price-threshold-c-per-kwh` default `10.0`
- `--discharge-required-cheap-hours` default `4.0`

`--charge-watts` is the inverter command used during charging. `--planner-charge-kwh-per-minute` is the battery fill rate used for energy and cost planning.

The discharge override only counts cheap future recharge minutes where `general_per_kwh` is below the configured threshold and the interval is **not** in a demand window.

## Logging

Runtime logs are written as **NDJSON**.

Useful planner fields include:

- `required_energy_kwh`
- `required_charge_minutes`
- `planned_charge_kwh`
- `planned_charge_minutes`
- `estimated_total_charge_cost`
- `average_planned_charge_price_c_per_kwh`
- `average_planned_effective_price_c_per_kwh`
- `selected_minute_count`
- `target_reachable`
- `plan_hourly_actions`
- `next_charge_at`

## Running

Install dependencies:

```bash
make install
```

Check the script:

```bash
make check
```

Run the live controller loop:

```bash
make run
```

Run it without applying changes:

```bash
make dry-run
```
