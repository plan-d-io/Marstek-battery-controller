# Marstek Battery Controller

[![GitHub Release](https://img.shields.io/github/v/release/plan-d-io/Marstek-battery-controller)](https://github.com/plan-d-io/Marstek-battery-controller/releases)
[![GitHub Issues](https://img.shields.io/github/issues/plan-d-io/Marstek-battery-controller)](https://github.com/plan-d-io/Marstek-battery-controller/issues)
[![Downloads](https://img.shields.io/github/downloads/plan-d-io/Marstek-battery-controller/total)](https://github.com/plan-d-io/Marstek-battery-controller/releases)

Custom Home Assistant integration that controls a **Marstek Venus E** home battery for **self-consumption**, **peak consumption compensation** with optional **pre-peak grid charging**, and full **manual control**. It's meant as a more robust replacement for the default Marstek control, which can be spotty, also offering additional features like preserving some energy to avoid consumption peaks (e.g. during the evening, where under normal control the battery could already be empty).

This integration leverages the [**`marstek_modbus`** integration](https://github.com/ViperRNMC/marstek_venus_modbus) by [ViperRNMC](https://github.com/ViperRNMC), which takes care of the Modbus communication to the Marstek battery. 

Currently, this integration is just a fancy scheduler. Everything it does can also be done using automations and template sensors, or NodeRED. However if you're looking for an easy solution and are using HACS, this integration is perhaps for you. Click, install, it works.

## Why this integration exists

In Flanders, many households pay a **capacity tariff**: your bill depends not only on energy (kWh) but on the **highest 15-minute average power** each month, then averaged over 12 months. A single short spike can cost on the order of **€50 per kW per year** — so shaving peaks saves real money.

The stock Marstek control aims at self-consumption but does not know your capacity-tariff budget or **anticipates** daily peaks (evening or morning). This integration adds capacity-tariff awareness and optional **boost charging** (grid pre-charge before your peak window) or **reserve** (hold SoC from solar only).

You can use it outside Flanders too: turn off the capacity-tariff guard and run plain self-consumption or timed boost/reserve behaviour in any region.

## Requirements

- Home Assistant `2025.9` or later
- HACS installed (for HACS install path)
- The [ViperRNMC `marstek_modbus`](https://github.com/ViperRNMC/marstek_venus_modbus) integration installed and connected to your Marstek Venus E
- A grid power measurement (see *A note on grid-power update rates* below)

**Recommended:**

- A P1 dongle or other grid power sensor with an update rate of at least 1 HZ. See note and recommendations below
- Marstek Venus E firmware v144 or later (Modbus over wired Ethernet, no RS-485 converter required)
- Marstek Venus E connected over wired Ethernet

## A note on grid-power update rates

Home Assistant does not allow official integrations to have a polling rate faster than **5 seconds**. As such, many digital-meter integrations only update every **5–10 seconds** or so. That is often too slow for smooth battery control: the controller reacts to load changes that are already seconds old, leading to overshoot and oscillations.

This integration addresses the **battery** side by offering, during config flow, to set the `marstek_modbus` integration's **high-priority polling to 1 second** (within what Modbus and the inverter allow). I have extensively tested this on my setup, and have not encountered any issues with dropped commands.

For the **grid** side you have three practical options:

1. **HomeWizard P1 dongle.** The integration can auto-detect HomeWizard P1 devices during setup and poll the dongle's local HTTP API at **1 Hz**, bypassing Home Assistant's sensor polling. Best behaviour with minimal setup.

2. **Push-based meter dongles.** Dongles that push readings over MQTT (or similar) can deliver sub-second or 1 Hz updates without HA's polling limit. The [plan-d P1 dongle](https://github.com/plan-d-io/P1-dongle) is one example (1 Hz over MQTT).

3. **A self-built fast sensor.** E.g. a REST-based template sensor in `configuration.yaml` with `scan_interval: 1` hitting your meter's local API can produce 1-second updates; select that entity as the grid power sensor. 

If you only have a **5–10 s** grid sensor, the integration still works, but expect more setpoint movement. Widening the smoothing windows (e.g. 10 s) reduces noise at the cost of slower response.

## Installation
Make sure you have [ViperRNMC `marstek_modbus` integration](https://github.com/ViperRNMC/marstek_venus_modbus) installed first!

### HACS

1. Open HACS → **Integrations** → **⋮** → **Custom repositories**.
2. Add `https://github.com/plan-d-io/Marstek-battery-controller` as category **Integration**.
3. Install **Marstek Battery Controller** and restart Home Assistant.

### Manual

Copy the folder `custom_components/marstek_battery_controller/` into your Home Assistant `config/custom_components/` directory, restart HA, then add the integration from the UI.

## Setup

1. Go to **Settings → Devices & services → Add integration**.
2. Search for **Marstek Battery Controller** and start the configuration flow.

### Configuration flow overview

| Step | What it asks | What to choose |
|------|--------------|----------------|
| Marstek battery | Which Marstek Venus E to control | Pick the one detected via `marstek_modbus`, or enable manual entity setup if discovery doesn't work |
| Speed up battery polling | Whether to set `marstek_modbus` to 1-second polling | Recommended: yes. The setting can be changed later in the `marstek_modbus` integration's options |
| Grid power source | Where to read household grid power from | If you have a HomeWizard P1, pick that (recommended). Otherwise pick an existing power sensor or enter a HomeWizard IP manually |
| Optional sensors | Two extra inputs for capacity tariff handling | The current quarter-hour average power sensor (typical name: peak demand, kwartiervermogen) and optionally a monthly peak sensor. Both optional — leave empty if you don't have them |
| Initial parameters | Starting values for SoC limits, peak window time, etc. | Defaults are reasonable; everything is editable later via the device page |

### Day-to-day parameters

These are the parameters most users adjust occasionally:

- **Mode** — switch between modes as your needs change (e.g. enable boost during winter, switch back to plain self-consumption in summer)
- **Minimum SoC** / **Maximum SoC** — battery operating range
- **Peak window start** — when your evening (or morning) peak typically begins
- **Reserve target SoC** — how much battery you want available before the peak window starts
- **Capacity tariff limit** — your monthly peak budget in watts (the integration won't let the battery push you over)

The remaining parameters (smoothing windows, send interval, battery capacity, boost charge power) are advanced and are filed under **Configuration** on the device page.

### Validation rules

**Minimum SoC** must be less than **maximum SoC**; **boost charge power** and **manual power** must not exceed **maximum battery power** (enforced in the options flow). If **reserve protection start** is the same time as **peak window start**, the reserve protection window is empty and a warning is logged.

## Operating modes

- **Released** — relinquishes control back to the Marstek app / native logic.
- **Self-consumption** — textbook self-consumption: charge with surplus solar that would otherwise be exported, discharge to cover household consumption, all within configurable SoC limits.
- **Self-consumption + boost** — like self-consumption, but also actively charges the battery from the grid before your configured peak window if it can't reach the reserve target SoC from solar alone. Useful in regions with capacity tariffs to ensure the battery is loaded before the daily consumption peak.
- **Self-consumption + reserve** — like self-consumption, but stops discharging below the reserve target SoC during the protection window. Charges from solar surplus only; never from the grid. Useful when you want to keep some battery in reserve for the evening but don't want to pay grid-charge costs.
- **Manual** — directly charge or discharge to a target SoC at a chosen power. Auto-exits to the previously active mode when the target is reached.

## Which mode should I pick?

- I just want maximum solar self-use, no capacity tariff to worry about → **Self-consumption**
- I have a capacity tariff and I'm OK pre-charging the battery from the grid before my peak window if needed (max captar is respected) → **Self-consumption + boost**
- I have a capacity tariff but I want the battery to charge from solar only — never from the grid → **Self-consumption + reserve**
- I want to manually charge or discharge to a specific SoC → **Manual**
- I want the Marstek app / Marstek's own logic to control the battery → **Released**

## Entities

The integration creates a single device with the following entities, organised by where they appear on the device page.

### Controls

| Entity | Purpose |
|--------|---------|
| Mode | The active operating mode |
| Minimum SoC | Battery won't discharge below this |
| Maximum SoC | Battery won't charge above this |
| Maximum battery power | Hard ceiling on battery power, both directions |
| Capacity tariff enabled | Whether capacity-tariff logic is active |
| Capacity tariff limit | Your monthly peak budget in watts |
| Reserve target SoC | The SoC the boost / reserve modes try to ensure for the peak window |
| Boost charge power | How fast to charge from the grid in boost mode |
| Peak window start | When your daily peak window begins (HH:MM, time of day) |
| Reserve protection start | When the reserve mode starts protecting the SoC floor (HH:MM) |
| Manual target SoC | Target SoC for manual mode |
| Manual power | Power to apply in manual mode |
| Manual trigger | Press to activate manual mode with the configured target / power |

### Sensors (main)

| Entity | Purpose |
|--------|---------|
| Status | High-level mode the integration is currently in (`Self-consumption`, `Boost charging`, `Reserve held`, etc.) |
| Last sent setpoint | The actual signed-watts command the integration last wrote to the battery (positive = discharge, negative = charge) |

### Sensors (diagnostic)

These are useful for understanding behaviour but most users never need to look at them. They live in the Diagnostic section of the device page.

| Entity | Purpose |
|--------|---------|
| Detail | More specific reason the controller is at its current setpoint (e.g. `At minimum SoC`, `Peak limit reached`) |
| Target setpoint | The setpoint the calculator wants — usually equal to Last sent setpoint, can briefly differ |
| Active capacity tariff limit | Effective threshold (max of your set limit and the current monthly peak, when known) |
| Boost must start by | Time at which the boost mode must start charging to reach the reserve target by the peak window |
| Minutes to peak window | Countdown to the next peak window start |
| Energy needed for reserve | Wh required to reach the reserve target SoC from current SoC |
| Grid power smoothed | Internal smoothed value used by the calculator |
| Battery power smoothed | Internal smoothed value used by the calculator |
| Grid power fast (HomeWizard) | Real-time grid power from the HomeWizard fast-poll, when active |

### Configuration

These appear under the Configuration section of the device page. They're advanced tuning parameters — change carefully.

| Entity | Purpose |
|--------|---------|
| Battery command send interval | How often the integration writes to the battery (default 5 s) |
| Grid power averaging window | Smoothing window for the grid input (default 5 s) |
| Battery power averaging window | Smoothing window for the battery input (default 5 s) |
| Battery capacity | Total capacity in Wh used for energy/time calculations |

## Device-native alternative (not used here)

Marstek firmware exposes Modbus register **42011** (`charge_to_soc`) as a built-in way to charge to a target SoC. **This integration does not use that register**; manual mode and auto-exit are implemented explicitely to retain full visibility of what the battery is doing.

## Localization

UI strings for the integration (config flow, entity names, mode states, issues) are provided in **English**, **English (GB)**, **Dutch**, **French**, and **German** under `custom_components/marstek_battery_controller/translations/`.

## License

This work is licensed under a Creative Commons (4.0 International License): **Attribution**
