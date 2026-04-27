# Marstek Battery Controller

[![GitHub Release](https://img.shields.io/github/v/release/plan-d-io/Marstek-battery-controller)](https://github.com/plan-d-io/Marstek-battery-controller/releases)
[![GitHub Issues](https://img.shields.io/github/issues/plan-d-io/Marstek-battery-controller)](https://github.com/plan-d-io/Marstek-battery-controller/issues)
[![Downloads](https://img.shields.io/github/downloads/plan-d-io/Marstek-battery-controller/total)](https://github.com/plan-d-io/Marstek-battery-controller/releases)

Custom Home Assistant integration that controls a **Marstek Venus E** home battery for **self-consumption**, **peak consumption compensation** with optional **pre-evening grid charging**, and full **manual control**. It's meant as a more robust replacement for the default Marstek control, which can be spotty, also offering additional features like preserving some energy to avoid consumption peaks (e.g. during the evening, where under normal control the battery could already be empty).

This integration leverages the [**`marstek_modbus`** integration](https://github.com/ViperRNMC/marstek_venus_modbus) by [ViperRNMC](https://github.com/ViperRNMC), which takes care of the Modbus communication to the Marstek battery. 

Everything this integration does can also be done using automations and template sensors, or NodeRED, but if you're looking for an easy solution and are using HACS, this integration is perhaps for you.

## Screenshots

*(Placeholder — add dashboard screenshots after deployment.)*

## Requirements

- Home Assistant `2025.9` or later
- HACS installed
- [ViperRNMC `marstek_modbus` integration](https://github.com/ViperRNMC/marstek_venus_modbus) installed
- Grid power measurements (preferably a P1 dongle)

Recommended:
- Marstek Venus E firmware v144 or later
- Marstek Venus E connected over wired ethernet

From firmware v144, the Modbus interface is also exposed over the wired ethernet port, removing the need for RS-485 converters like the Elfin. 


## Operating modes

- **Released**: relinquishes control back to the Marstek app
- **Self-consumption**: text-book self-consumption: charge with excess power that would otherwise be injected into the grid until SoC reaches max value, discharge to compensate power draw from the grid untill SoC reaches the min value
- **Self-consumption + evening peak boost**
- **Self-consumption + passive evening peak**
- **Manual**

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

| Step | What you configure |
|------|---------------------|
| **Battery device** | Pick the device discovered from **`marstek_modbus`**, or enable **manual setup** and map **six entities** (`battery_soc`, `ac_power`, RS485 control, force mode select, set charge power, set discharge power`). Those entities are created by **`marstek_modbus`**—the controller only **calls** them; manual mode exists when discovery is unavailable. |
| **Grid power** | **Required.** Any **sensor with device class Power** (W)—e.g. digital meter / P1 / HomeWizard / SlimmeLezer. Sign convention: **positive = importing from grid**, **negative = exporting**. |
| **Optional sensors** | Peak demand **this quarter-hour** (`cap_now`) and optional **monthly peak** sensor for capacity-tariff ceiling logic (W). Compatible with integrations that expose suitable power sensors (e.g. DSMR aggregates or projects like **[Peak Power Forecast](https://github.com/Epyon01P/Peak-Power-Forecast)**). |
| **Initial parameters** | Defaults for SoC limits, smoothing windows, battery capacity, evening limits, capacity tariff flag, times (HH:MM), etc. **Mode defaults to Released** (no mode picker on first setup). Everything stays editable via entities and **Integrations → Configure → Options**. |

Validation rules: **min SoC < max SoC**; **evening max charge ≤ max battery power**; **manual power ≤ max battery power** (options flow). If **passive floor start** equals **evening peak start**, the passive protection window is empty and a warning is logged.

## Entities

### Select

| Entity | Purpose |
|--------|---------|
| **Mode** | `released`, `self_consumption`, `self_consumption_evening_peak`, `self_consumption_passive_evening_peak`, `manual` (labels are translated). |

### Numbers

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

### Sensors

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

Marstek firmware exposes Modbus register **42011** (`charge_to_soc`) as a built-in way to charge to a target SoC. **This integration does not use that register**; manual mode and auto-exit are implemented explicitely to retain full visibility of what the battery is doing.

## Localization

UI strings for the integration (config flow, entity names, mode states, issues) are provided in **English**, **English (GB)**, **Dutch**, **French**, and **German** under `custom_components/marstek_battery_controller/translations/`.

## License

This work is licensed under a Creative Commons (4.0 International License): **Attribution**