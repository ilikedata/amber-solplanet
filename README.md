# Amber Electric Solplanet Inverter/Battery Controller

## Introduction

This repository contains a small local controller for **Solplanet battery/inverter systems** on **Amber Electric** pricing.

It plans **grid charging only** from the Amber forecast at the **lowest forecast cost**, while excluding the daily demand window.

## What The Controller Does

On each loop, the controller:

1. reads battery telemetry from the local Solplanet API
2. fetches the Amber forecast horizon
3. calculates required battery energy to reach the target SOC
4. converts the planner charge rate into **kWh per minute**
5. selects the cheapest non-demand-window Amber intervals until that energy is covered
6. allows a **partial final interval** for the last bit of required energy
7. decides whether the current interval should `charge` or `fallback`
8. applies only the current short-lived action using the local Solplanet control API

The fallback action is **self-consumption mode**.

## Control Approach

The planner is intentionally minimal.

It uses:

- Amber forecast prices only
- actual Amber interval durations exactly as returned
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
- `--planner-interval-minutes` default `5`
- `--charge-watts` default `15000`
- `--planner-charge-kwh-per-minute` default `0.1693`
- `--charge-target-soc` default `97`

`--charge-watts` is the inverter command used during charging. `--planner-charge-kwh-per-minute` is the battery fill rate used for energy and cost planning.

## Logging

Runtime logs are written as **NDJSON**.

Useful planner fields include:

- `required_energy_kwh`
- `required_charge_minutes`
- `planned_charge_kwh`
- `planned_charge_minutes`
- `estimated_total_charge_cost`
- `average_planned_charge_price_c_per_kwh`
- `selected_interval_count`
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
