# Marstek Battery Controller

Home Assistant custom integration that orchestrates a **Marstek Venus E** home battery for self-consumption, optional pre-evening grid charging, passive evening floor protection, and a manual override. Control is applied through the **ViperRNMC `marstek_modbus`** integration (install via HACS) using standard Home Assistant service calls—this integration does not open Modbus registers itself.

**Author:** Joannes Laveyne · [Epyon01P](https://github.com/Epyon01P) · [Plan-D](https://github.com/plan-d-io)

## Screenshots

*(Placeholder — add dashboard screenshots after deployment.)*

## Requirements

- Home Assistant **2025.9** or newer  
- **`marstek_modbus`** installed and configured for your Venus E device  

## Installation

### HACS

1. Open HACS → **Integrations** → **⋮** → **Custom repositories**.  
2. Add repository `https://github.com/plan-d-io/Marstek-battery-controller` as **Integration**.  
3. Install **Marstek Battery Controller** and restart Home Assistant.  
4. Add the integration via **Settings → Devices & services → Add integration**.

### Manual

Copy the folder `custom_components/marstek_battery_controller/` into your Home Assistant `config/custom_components/` directory, restart HA, then add the integration from the UI.

## Configuration walkthrough

1. **Battery device** — Pick the device discovered from `marstek_modbus`, or configure all six entity roles manually if discovery is unavailable.  
2. **Grid power** — Required sensor: power in **W**, **positive = importing from grid**.  
3. **Optional sensors** — Optional 15‑minute rolling average grid power (`cap_now`) and optional monthly peak sensor for the capacity-tariff ceiling.  
4. **Initial parameters** — Defaults for mode, SoC limits, smoothing windows, evening/passive times (HH:MM), etc. Everything remains editable from entities and the **Options** flow.

Parameters enforce **min SoC** strictly below **max SoC**, cap **evening max charge** and **manual power** at **max battery power**, and log a warning if passive floor start equals evening peak (empty protection window).

## Entities

### Select

| Entity | Purpose |
|--------|---------|
| **Mode** | `released`, `self_consumption`, `self_consumption_evening_peak`, `self_consumption_passive_evening_peak`, `manual` |

### Numbers (§7)

| Entity | Purpose |
|--------|---------|
| Min SoC (discharge floor) | Lower SoC limit for discharge guard |
| Max SoC (charge ceiling) | Upper SoC limit for charge guard |
| Max battery power | Absolute power clamp |
| Send interval | Seconds between Modbus write ticks |
| Grid / battery smoothing window | Sliding window length (s) for inputs |
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

### Datetime (time of day)

| Entity | Purpose |
|--------|---------|
| Evening peak start | End of boost/protection windows |
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
| Latest start charge | §10 laadplanning (`datetime` or unavailable when `no_need`) |
| Effective cap threshold | Dynamic W threshold (user max vs monthly peak) |
| Grid power smoothed | Smoothed grid W |
| Battery power smoothed | Smoothed battery AC W |
| Cap now (internal) | Only if no user `cap_now` sensor — internal 15‑min mean |
| Minutes to evening peak | Minutes until next evening peak time |
| Energy needed for evening | Wh to reach evening min SoC (floor 0) |

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

## License

See repository metadata (add a `LICENSE` file if you publish publicly).
