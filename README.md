# Electricity Controller

This repository contains a small local controller for **Solplanet battery/inverter systems** on **Amber Electric** pricing.

It exists for a specific use case: you want battery behavior to respond to Amber price signals, but you do not want to depend on Home Assistant or a larger automation stack to do it.

## Problem

Solplanet exposes a local HTTP API, and Amber exposes current price and tariff metadata, but there is no simple built-in controller that ties the two together for:

- charging on very cheap import pricing
- discharging on favorable feed-in pricing
- respecting demand windows
- keeping battery SOC within practical bounds
- failing safely when communications are unreliable

This repository is that controller.

## What The Controller Does

On each loop, the controller:

1. reads battery telemetry from the local Solplanet API
2. reads current Amber price intervals
3. decides whether to `charge`, `discharge`, or `fallback`
4. applies that decision using the local Solplanet control API

The fallback action is **self-consumption mode**.

## Control Approach

The controller is intentionally simple and conservative.

It uses:

- **Amber general pricing** to decide when charging is allowed
- **Amber feed-in pricing** to decide when discharge/export is attractive
- **battery SOC guards** to stop charge/discharge outside configured bounds
- **demand-window guards** so charge windows do not run into the daily demand period
- **short, backdated schedule windows** so stale commands naturally expire quickly if comms fail

That last point matters: this project is designed around the reality that inverter control can be unreliable. The scheduling strategy reduces the blast radius of missed updates.

## Why Someone Else Might Use This

If you have the same ingredients:

- Solplanet inverter with local API access
- Amber Electric pricing
- interest in battery arbitrage or price-aware battery control

then this repository gives you a working starting point for local control without having to reverse-engineer the Solplanet control path from scratch.

## Configuration

The controller reads local configuration from `.env`:

- `SOLPLANET_HOST`
- `SOLPLANET_BATTERY_SN`
- `AMBER_SITE_ID`
- `AMBER_API_KEY`

See [`.env.example`](/home/tai/svn/electricity/.env.example).

## Logging

Runtime logs are written as **NDJSON** so the output can be analysed later for controller behavior and battery/arbitrage history.

Typical event types include:

- `battery_state`
- `decision`
- `dry_run`
- `applied`
- `error`

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
