# Marstek Battery Controller

[![GitHub Release](https://img.shields.io/github/v/release/plan-d-io/Marstek-battery-controller)](https://github.com/plan-d-io/Marstek-battery-controller/releases)
[![GitHub Issues](https://img.shields.io/github/issues/plan-d-io/Marstek-battery-controller)](https://github.com/plan-d-io/Marstek-battery-controller/issues)
[![Downloads](https://img.shields.io/github/downloads/plan-d-io/Marstek-battery-controller/total)](https://github.com/plan-d-io/Marstek-battery-controller/releases)

Custom Home Assistant integration that orchestrates a **Marstek Venus E** home battery for **self-consumption**, optional **pre-evening grid charging**, **passive evening floor protection**, and a **manual override**.

Control is applied **only via standard Home Assistant services** (`switch`, `select`, `number`) on entities exposed by the **`marstek_modbus`** integration—the controller does **not** open Modbus TCP/RTU sockets itself.

All controller entities are grouped under **one device** (with optional linkage to your Marstek hardware when discovery is used). Translation files are shipped for **English**, **English (GB)**, **Dutch**, **French**, and **German** (`translations/`). **Important:** HA’s **Settings → System → General → Language** drives entity names from those files; align it with your profile language if labels look wrong.

## Screenshots

*(Placeholder — add dashboard screenshots after deployment.)*

## Requirements

| Requirement | Notes |
|-------------|--------|
| Home Assistant Core | **2025.9** or newer |
| **`marstek_modbus`** | Install via HACS and configure your Venus E device |

## Features

- **Operating modes:** Released, self-consumption, self-consumption + evening peak boost, self-consumption + passive evening peak, manual (see architecture spec §6).
- **Grid coupling:** Signed grid power (**W**) with smoothing; configurable send interval and power clamps.
- **Capacity tariff helpers:** Compare current quarter-hour demand (`cap_now`) against an effective ceiling (desired max peak vs optional monthly peak sensor).
- **Laadplanning (§10):** Computes latest start time for evening boost when pre-evening charging is needed.
- **Diagnostics:** Operating state, reason codes, smoothed powers, optional internal `cap_now` surrogate when no external sensor is configured.
- **Safety / robustness:** SoC guards, restart write grace, sensor-loss handling, repeated Modbus failure Repair issue (§16).

## Installation

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

| Step | What you configure |
|------|---------------------|
| **Battery device** | Pick the device discovered from **`marstek_modbus`**, or enable **manual setup** and map **six entities** (`battery_soc`, `ac_power`, RS485 control, force mode select, set charge power, set discharge power`). Those entities are created by **`marstek_modbus`**—the controller only **calls** them; manual mode exists when discovery is unavailable. |
| **Grid power** | **Required.** Any **sensor with device class Power** (W)—e.g. digital meter / P1 / HomeWizard / SlimmeLezer. Sign convention: **positive = importing from grid**, **negative = exporting**. |
| **Optional sensors** | Peak demand **this quarter-hour** (`cap_now`) and optional **monthly peak** sensor for capacity-tariff ceiling logic (W). Compatible with integrations that expose suitable power sensors (e.g. DSMR aggregates or projects like **[Peak Power Forecast](https://github.com/Epyon01P/Peak-Power-Forecast)**). |
| **Initial parameters** | Defaults for SoC limits, smoothing windows, battery capacity, evening limits, capacity tariff flag, times (HH:MM), etc. **Mode defaults to Released** (no mode picker on first setup). Everything stays editable via entities and **Integrations → Configure → Options**. |

Validation rules: **min SoC < max SoC**; **evening max charge ≤ max battery power**; **manual power ≤ max battery power** (options flow). If **passive floor start** equals **evening peak start**, the passive protection window is empty and a warning is logged.

### Controls vs configuration (UI)

Home Assistant splits the device card into **Controls** (primary interaction) and **Configuration** (advanced parameters) using **entity categories**.

- **Controls:** mode select, max desired 15‑min peak, manual target SoC, manual power, manual trigger button.
- **Configuration:** remaining numbers (limits, smoothing, battery capacity, evening settings), capacity tariff switch, evening peak start / passive floor‑protection start (**time** entities—clock only, no date picker).

### Translation / language notes

- Translation files: `en.json`, `en-GB.json`, `nl.json`, `fr.json`, `de.json`.
- If friendly names do not match your profile language, check **Settings → System → General → Language** as well as your **user profile** language (HA resolves integration strings from the general language in many views).

## Entities

### Select

| Entity | Purpose |
|--------|---------|
| **Mode** | `released`, `self_consumption`, `self_consumption_evening_peak`, `self_consumption_passive_evening_peak`, `manual` (labels are translated). |

### Numbers (§7)

| Entity | Purpose |
|--------|---------|
| Min SoC (discharge floor) | Lower SoC limit for discharge guard |
| Max SoC (charge ceiling) | Upper SoC limit for charge guard |
| Max battery power | Absolute power clamp |
| Send battery command interval | Seconds between Modbus write ticks |
| Grid / battery power averaging window | Sliding window length (s) for inputs |
| Battery capacity | Wh (laadplanning) |
| Evening min SoC | Target SoC before evening peak |
| Evening max charge power | Grid boost charge power cap |
| Max desired 15‑min peak | Base capacity-tariff ceiling (W) |
| Manual target SoC | Manual mode target |
| Manual power | Manual charge/discharge magnitude |

### Switch

| Entity | Purpose |
|--------|---------|
| Capacity tariff enabled | Enables capacity-tariff comparisons for boost/floor logic |

### Time (time of day)

Wall-clock controls (**no date picker**):

| Entity | Purpose |
|--------|---------|
| Evening peak start | End of boost / passive protection window reference |
| Passive floor-protection start | Start of passive floor window |

### Button

| Entity | Purpose |
|--------|---------|
| Manual trigger | Validates target ≠ current SoC and enters manual mode |

### Sensors (diagnostics, §14)

| Entity | Purpose |
|--------|---------|
| Target setpoint | Calculator output (W, signed) |
| Last sent setpoint | Last value written via services |
| Operating state | `released`, `self_consumption`, `pre_charging`, `floor_protection`, `manual_charging`, `manual_discharging` |
| Reason code | `normal`, `at_floor`, `at_ceiling`, `cap_tariff`, `boost_active`, `floor_held`, `manual_active`, `released` |
| Latest start charge | §10 laadplanning (`datetime` state or unavailable when `no_need`) |
| Effective capacity threshold | Dynamic W threshold (max desired peak vs monthly peak when valid) |
| Grid power smoothed | Smoothed grid W |
| Battery power smoothed | Smoothed battery AC W |
| Cap now (internal) | Only when **no** optional `cap_now` sensor configured—internal rolling mean substitute |
| Minutes to evening peak | Minutes until next evening peak time |
| Energy needed for evening | Wh to reach evening min SoC (floored at 0) |

## Device-native alternative (not used here)

Marstek firmware exposes Modbus register **42011** (`charge_to_soc`) as a built-in way to charge to a target SoC. **This integration does not use that register**; manual mode and auto-exit are implemented in Python per the architecture specification (§6.6, §13.2).

## Troubleshooting

| Symptom | What to check |
|---------|----------------|
| Integration fails at setup | `marstek_modbus` running; device has entities with `unique_id` suffixes `battery_soc`, `ac_power`, `rs485_control_mode`, `force_mode`, `set_charge_power`, `set_discharge_power`. |
| “Roles not resolved” | Use manual entity mapping in the config flow. |
| No writes | Mode `released`, or within **60 s** restart grace; check logs for grace completion. |
| Frequent Released | Grid, SoC, or battery AC unavailable **> 60 s** — integration forces Released (§16). |
| Writes fail | Same setpoint retried each tick; after **5** failures an **issue** is raised (§16). |
| Boost never starts | Laadplanning returned `no_need` (SoC ≥ evening min) or outside `[latest_start, evening_peak)`. |

Enable **debug** logging for `custom_components.marstek_battery_controller` to see each calculation and Modbus sequence (§18).

## Development / tests

```bash
python -m pytest tests/components/marstek_battery_controller/
```

Unit tests cover `calculator.py`, `smoothing.py`, and laadplanning (§10) without requiring Home Assistant to be installed.

## Localization

UI strings for the integration (config flow, entity names, mode states, issues) are provided in **English**, **English (GB)**, **Dutch**, **French**, and **German** under `custom_components/marstek_battery_controller/translations/`.

## License

This work is licensed under a Creative Commons (4.0 International License): **Attribution**