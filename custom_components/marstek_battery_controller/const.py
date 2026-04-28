"""Constants and defaults for the Marstek Battery Controller integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "marstek_battery_controller"

# Storage
STORAGE_KEY: Final = DOMAIN
STORAGE_VERSION: Final = 1

# Discovery — unique_id suffixes on marstek_modbus entities (language-independent).
ROLE_BATTERY_SOC: Final = "battery_soc"
ROLE_AC_POWER: Final = "ac_power"
ROLE_RS485_CONTROL: Final = "rs485_control_mode"
ROLE_FORCE_MODE: Final = "force_mode"
ROLE_SET_CHARGE_POWER: Final = "set_charge_power"
ROLE_SET_DISCHARGE_POWER: Final = "set_discharge_power"

ROLE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ROLE_BATTERY_SOC,
        ROLE_AC_POWER,
        ROLE_RS485_CONTROL,
        ROLE_FORCE_MODE,
        ROLE_SET_CHARGE_POWER,
        ROLE_SET_DISCHARGE_POWER,
    }
)

# Operating modes (English keys, translated labels).
MODE_RELEASED: Final = "released"
MODE_SELF_CONSUMPTION: Final = "self_consumption"
MODE_SELF_CONSUMPTION_EVENING_PEAK: Final = "self_consumption_evening_peak"
MODE_SELF_CONSUMPTION_PASSIVE_EVENING_PEAK: Final = (
    "self_consumption_passive_evening_peak"
)
MODE_MANUAL: Final = "manual"

MODES: Final[tuple[str, ...]] = (
    MODE_RELEASED,
    MODE_SELF_CONSUMPTION,
    MODE_SELF_CONSUMPTION_EVENING_PEAK,
    MODE_SELF_CONSUMPTION_PASSIVE_EVENING_PEAK,
    MODE_MANUAL,
)

# Force mode values for marstek_modbus select entity (neutral option is ``stop`` or
# ``standby`` depending on integration — resolved at runtime in modbus_writer).
FORCE_CHARGE: Final = "charge"
FORCE_DISCHARGE: Final = "discharge"

# Diagnostic — operating_state (sensor text).
STATE_RELEASED: Final = "released"
STATE_SELF_CONSUMPTION: Final = "self_consumption"
STATE_PRE_CHARGING: Final = "pre_charging"
STATE_FLOOR_PROTECTION: Final = "floor_protection"
STATE_MANUAL_CHARGING: Final = "manual_charging"
STATE_MANUAL_DISCHARGING: Final = "manual_discharging"

# Diagnostic — reason_code (sensor text).
REASON_NORMAL: Final = "normal"
REASON_AT_FLOOR: Final = "at_floor"
REASON_AT_CEILING: Final = "at_ceiling"
REASON_CAP_TARIFF: Final = "cap_tariff"
REASON_BOOST_ACTIVE: Final = "boost_active"
REASON_FLOOR_HELD: Final = "floor_held"
REASON_MANUAL_ACTIVE: Final = "manual_active"
REASON_RELEASED: Final = "released"

# Laadplanning sentinel when no grid charge is needed.
LATEST_START_NO_NEED: Final = "no_need"

# Config flow / options keys
CONF_MARSTEK_DEVICE_ID: Final = "marstek_device_id"
CONF_MANUAL_ENTITIES: Final = "manual_entities"
CONF_GRID_POWER: Final = "grid_power_entity"
CONF_CAP_NOW_SENSOR: Final = "cap_now_sensor"
CONF_MONTHLY_PEAK_SENSOR: Final = "monthly_peak_sensor"
CONF_USE_DISCOVERY: Final = "use_discovery"

# Manual entity keys (when not using discovery)
CONF_ENTITY_BATTERY_SOC: Final = "battery_soc_entity"
CONF_ENTITY_AC_POWER: Final = "ac_power_entity"
CONF_ENTITY_RS485: Final = "rs485_control_entity"
CONF_ENTITY_FORCE_MODE: Final = "force_mode_entity"
CONF_ENTITY_SET_CHARGE: Final = "set_charge_power_entity"
CONF_ENTITY_SET_DISCHARGE: Final = "set_discharge_power_entity"

# Defaults (§7)
DEFAULT_MODE: Final = MODE_RELEASED
DEFAULT_MIN_SOC: Final = 12.0
DEFAULT_MAX_SOC: Final = 100.0
DEFAULT_MAX_BATTERY_POWER: Final = 2500.0
DEFAULT_SEND_INTERVAL: Final = 5.0
DEFAULT_GRID_SMOOTHING_WINDOW: Final = 5.0
DEFAULT_BATTERY_SMOOTHING_WINDOW: Final = 5.0
DEFAULT_BATTERY_CAPACITY_WH: Final = 5120.0
DEFAULT_EVENING_PEAK_START_HOUR: Final = 18
DEFAULT_EVENING_PEAK_START_MINUTE: Final = 0
DEFAULT_EVENING_MIN_SOC: Final = 50.0
DEFAULT_EVENING_MAX_CHARGE_POWER: Final = 1250.0
DEFAULT_PASSIVE_FLOOR_START_HOUR: Final = 13
DEFAULT_PASSIVE_FLOOR_START_MINUTE: Final = 0
DEFAULT_CAPACITY_TARIFF_ENABLED: Final = True
DEFAULT_MAX_DESIRED_PEAK_W: Final = 2500.0
DEFAULT_MANUAL_TARGET_SOC: Final = 50.0
DEFAULT_MANUAL_POWER: Final = 1000.0

# Ranges (§7)
MIN_BATTERY_POWER_W: Final = 100.0
MAX_BATTERY_POWER_LIMIT_W: Final = 2500.0
MIN_SOC_PCT: Final = 0.0
MAX_SOC_PCT: Final = 100.0
MIN_SEND_INTERVAL_S: Final = 1.0
MAX_SEND_INTERVAL_S: Final = 60.0
MIN_SMOOTHING_WINDOW_S: Final = 1.0
MAX_SMOOTHING_WINDOW_S: Final = 300.0
MIN_BATTERY_CAPACITY_WH: Final = 800.0
MAX_BATTERY_CAPACITY_WH: Final = 15360.0
MIN_EVENING_CHARGE_POWER_W: Final = 100.0
MIN_MAX_DESIRED_PEAK_W: Final = 100.0
MAX_MAX_DESIRED_PEAK_W: Final = 10000.0

# Internal cap_now rolling window when user sensor absent (15 minutes).
CAP_NOW_INTERNAL_WINDOW_S: Final = 900

# Inter-write delay for Modbus (§12).
MODBUS_WRITE_DELAY_S: Final = 0.2

# Restart grace period before coordinator writes (§13.3).
RESTART_WRITE_GRACE_S: Final = 60

# Sensor unavailability forcing Released (§16).
SENSOR_UNAVAILABLE_RELEASE_S: Final = 60

# Consecutive Modbus failures before ERROR + repair (§16).
MODBUS_FAILURE_ERROR_THRESHOLD: Final = 5

ATTR_OPTION: Final = "option"

# Entity translation keys / unique suffixes for platforms
ENTITY_MODE: Final = "mode"
ENTITY_MIN_SOC: Final = "min_soc"
ENTITY_MAX_SOC: Final = "max_soc"
ENTITY_MAX_BATTERY_POWER: Final = "max_battery_power"
ENTITY_SEND_INTERVAL: Final = "send_interval"
ENTITY_GRID_SMOOTHING_WINDOW: Final = "grid_smoothing_window"
ENTITY_BATTERY_SMOOTHING_WINDOW: Final = "battery_smoothing_window"
ENTITY_BATTERY_CAPACITY: Final = "battery_capacity"
ENTITY_EVENING_PEAK_START: Final = "evening_peak_start"
ENTITY_EVENING_MIN_SOC: Final = "evening_min_soc"
ENTITY_EVENING_MAX_CHARGE_POWER: Final = "evening_max_charge_power"
ENTITY_PASSIVE_FLOOR_PROTECTION_START: Final = "passive_floor_protection_start"
ENTITY_CAPACITY_TARIFF_ENABLED: Final = "capacity_tariff_enabled"
ENTITY_MAX_DESIRED_PEAK: Final = "max_desired_peak"
ENTITY_MANUAL_TARGET_SOC: Final = "manual_target_soc"
ENTITY_MANUAL_POWER: Final = "manual_power"
ENTITY_MANUAL_TRIGGER: Final = "manual_trigger"

SENSOR_TARGET_SETPOINT: Final = "target_setpoint"
SENSOR_LAST_SENT_SETPOINT: Final = "last_sent_setpoint"
SENSOR_OPERATING_STATE: Final = "operating_state"
SENSOR_REASON_CODE: Final = "reason_code"
SENSOR_LATEST_START_CHARGE: Final = "latest_start_charge"
SENSOR_EFFECTIVE_CAP_THRESHOLD: Final = "effective_cap_threshold"
SENSOR_GRID_POWER_SMOOTHED: Final = "grid_power_smoothed"
SENSOR_BATTERY_POWER_SMOOTHED: Final = "battery_power_smoothed"
SENSOR_CAP_NOW_INTERNAL: Final = "cap_now_internal"
SENSOR_MINUTES_TO_EVENING_PEAK: Final = "minutes_to_evening_peak"
SENSOR_ENERGY_NEEDED_EVENING: Final = "energy_needed_for_evening"

ISSUE_MODBUS_FAILURES: Final = "modbus_write_failures"
ISSUE_MARSTEK_MISSING: Final = "marstek_integration_missing"
