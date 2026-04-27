"""Config and options flows for Marstek Battery Controller."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import UnitOfPower
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.selector import SelectSelectorMode

from . import const
from .discovery import list_marstek_devices, resolve_roles_for_device

_LOGGER = logging.getLogger(__name__)

STEP_ID_MANUAL = "manual_devices"
STEP_ID_GRID = "grid"
STEP_ID_OPTIONAL = "optional"
STEP_ID_PARAMS = "parameters"


def _power_sensor_selector() -> selector.EntitySelector:
    """Grid power: device_class power, W."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain="sensor",
            device_class=SensorDeviceClass.POWER,
            unit_of_measurement="W",
        )
    )


def _manual_toggle_is_on(raw: Any) -> bool:
    """Normalize BooleanSelector payloads (frontend may vary by HA version)."""
    if raw is True:
        return True
    if raw is False or raw is None:
        return False
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "on", "yes", "1")
    return bool(raw)


def _optional_powerish_selector() -> selector.EntitySelector:
    """Optional sensor (often power)."""
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))


def _param_schema_defaults(
    merged: dict[str, Any],
) -> vol.Schema:
    """§5.4 — voluptuous schema with explicit defaults."""
    d = merged
    return vol.Schema(
        {
            vol.Optional("mode", default=d.get("mode", const.DEFAULT_MODE)): vol.In(
                const.MODES
            ),
            vol.Optional(
                "min_soc", default=d.get("min_soc", const.DEFAULT_MIN_SOC)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=const.MIN_SOC_PCT,
                    max=const.MAX_SOC_PCT,
                    step=1,
                    mode=selector.NumberSelectorMode.SLIDER,
                    unit_of_measurement="%",
                )
            ),
            vol.Optional(
                "max_soc", default=d.get("max_soc", const.DEFAULT_MAX_SOC)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=const.MIN_SOC_PCT,
                    max=const.MAX_SOC_PCT,
                    step=1,
                    mode=selector.NumberSelectorMode.SLIDER,
                    unit_of_measurement="%",
                )
            ),
            vol.Optional(
                "max_battery_power",
                default=d.get("max_battery_power", const.DEFAULT_MAX_BATTERY_POWER),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=const.MIN_BATTERY_POWER_W,
                    max=const.MAX_BATTERY_POWER_LIMIT_W,
                    step=50,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement=UnitOfPower.WATT,
                )
            ),
            vol.Optional(
                "send_interval",
                default=d.get("send_interval", const.DEFAULT_SEND_INTERVAL),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=const.MIN_SEND_INTERVAL_S,
                    max=const.MAX_SEND_INTERVAL_S,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
            vol.Optional(
                "grid_smoothing",
                default=d.get("grid_smoothing", const.DEFAULT_GRID_SMOOTHING_WINDOW),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=const.MIN_SMOOTHING_WINDOW_S,
                    max=const.MAX_SMOOTHING_WINDOW_S,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
            vol.Optional(
                "battery_smoothing",
                default=d.get(
                    "battery_smoothing", const.DEFAULT_BATTERY_SMOOTHING_WINDOW
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=const.MIN_SMOOTHING_WINDOW_S,
                    max=const.MAX_SMOOTHING_WINDOW_S,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
            vol.Optional(
                "battery_capacity",
                default=d.get("battery_capacity", const.DEFAULT_BATTERY_CAPACITY_WH),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=const.MIN_BATTERY_CAPACITY_WH,
                    max=const.MAX_BATTERY_CAPACITY_WH,
                    step=100,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="Wh",
                )
            ),
            vol.Optional(
                "evening_min_soc",
                default=d.get("evening_min_soc", const.DEFAULT_EVENING_MIN_SOC),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=const.MIN_SOC_PCT,
                    max=const.MAX_SOC_PCT,
                    step=1,
                    mode=selector.NumberSelectorMode.SLIDER,
                    unit_of_measurement="%",
                )
            ),
            vol.Optional(
                "evening_max_charge_power",
                default=d.get(
                    "evening_max_charge_power", const.DEFAULT_EVENING_MAX_CHARGE_POWER
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=const.MIN_EVENING_CHARGE_POWER_W,
                    max=const.MAX_BATTERY_POWER_LIMIT_W,
                    step=50,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement=UnitOfPower.WATT,
                )
            ),
            vol.Optional(
                "capacity_tariff_enabled",
                default=d.get(
                    "capacity_tariff_enabled", const.DEFAULT_CAPACITY_TARIFF_ENABLED
                ),
            ): selector.BooleanSelector(),
            vol.Optional(
                "max_desired_peak",
                default=d.get("max_desired_peak", const.DEFAULT_MAX_DESIRED_PEAK_W),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=const.MIN_MAX_DESIRED_PEAK_W,
                    max=const.MAX_MAX_DESIRED_PEAK_W,
                    step=50,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement=UnitOfPower.WATT,
                )
            ),
            vol.Optional(
                "manual_target_soc",
                default=d.get("manual_target_soc", const.DEFAULT_MANUAL_TARGET_SOC),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=const.MIN_SOC_PCT,
                    max=const.MAX_SOC_PCT,
                    step=1,
                    mode=selector.NumberSelectorMode.SLIDER,
                    unit_of_measurement="%",
                )
            ),
            vol.Optional(
                "manual_power",
                default=d.get("manual_power", const.DEFAULT_MANUAL_POWER),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=const.MIN_BATTERY_POWER_W,
                    max=const.MAX_BATTERY_POWER_LIMIT_W,
                    step=50,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement=UnitOfPower.WATT,
                )
            ),
            vol.Optional(
                "evening_peak_time",
                default=d.get("evening_peak_time", "18:00"),
            ): selector.TextSelector(),
            vol.Optional(
                "passive_floor_time",
                default=d.get("passive_floor_time", "13:00"),
            ): selector.TextSelector(),
        }
    )


class MarstekBatteryConfigFlow(ConfigFlow, domain=const.DOMAIN):
    """Handle UI configuration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow."""
        self._picked_device_id: str | None = None
        self._use_manual: bool = False
        self._manual_entities: dict[str, str] = {}
        self._grid_entity: str = ""
        self._cap_sensor: str | None = None
        self._monthly_sensor: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """§5.1 — device or manual."""
        devices = list_marstek_devices(self.hass)
        if user_input is not None:
            if _manual_toggle_is_on(user_input.get("manual")):
                self._use_manual = True
                self._picked_device_id = None
                return await self.async_step_manual_devices()
            device_id = user_input.get(const.CONF_MARSTEK_DEVICE_ID)
            if not device_id:
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._schema_user_pick(devices),
                    errors={"base": "device_required"},
                )
            try:
                resolved = resolve_roles_for_device(self.hass, str(device_id))
            except Exception:
                _LOGGER.exception(
                    "Role discovery raised for device_id=%s",
                    device_id,
                )
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._schema_user_pick(devices),
                    errors={"base": "discovery_failed"},
                )
            if resolved is None:
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._schema_user_pick(devices),
                    errors={"base": "discovery_failed"},
                )
            self._picked_device_id = str(device_id)
            self._use_manual = False
            return await self.async_step_grid()

        if not devices:
            self._use_manual = True
            return await self.async_step_manual_devices()

        return self.async_show_form(
            step_id="user",
            data_schema=self._schema_user_pick(devices),
        )

    def _schema_user_pick(self, devices: list[tuple[str, str]]) -> vol.Schema:
        """Device list must use SelectSelector value/label pairs (not vol.In(dict))."""
        device_options: list[dict[str, str]] = [
            {"value": did, "label": f"{name} ({did})"} for did, name in devices
        ]
        return vol.Schema(
            {
                vol.Optional("manual", default=False): selector.BooleanSelector(),
                vol.Optional(const.CONF_MARSTEK_DEVICE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=device_options,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )

    async def async_step_manual_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manual six-entity discovery fallback."""
        schema = vol.Schema(
            {
                vol.Required(const.CONF_ENTITY_BATTERY_SOC): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(const.CONF_ENTITY_AC_POWER): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(const.CONF_ENTITY_RS485): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                ),
                vol.Required(const.CONF_ENTITY_FORCE_MODE): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="select")
                ),
                vol.Required(const.CONF_ENTITY_SET_CHARGE): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="number")
                ),
                vol.Required(const.CONF_ENTITY_SET_DISCHARGE): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="number")
                ),
            }
        )
        if user_input is not None:
            self._manual_entities = user_input
            return await self.async_step_grid()

        return self.async_show_form(step_id=STEP_ID_MANUAL, data_schema=schema)

    async def async_step_grid(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """§5.2 — grid sensor."""
        schema = vol.Schema(
            {
                vol.Required(const.CONF_GRID_POWER): _power_sensor_selector(),
            }
        )
        if user_input is not None:
            self._grid_entity = user_input[const.CONF_GRID_POWER]
            return await self.async_step_optional()

        return self.async_show_form(step_id=STEP_ID_GRID, data_schema=schema)

    async def async_step_optional(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """§5.3 — optional sensors."""
        schema = vol.Schema(
            {
                vol.Optional(const.CONF_CAP_NOW_SENSOR): _optional_powerish_selector(),
                vol.Optional(const.CONF_MONTHLY_PEAK_SENSOR): _optional_powerish_selector(),
            }
        )
        if user_input is not None:
            self._cap_sensor = user_input.get(const.CONF_CAP_NOW_SENSOR)
            self._monthly_sensor = user_input.get(const.CONF_MONTHLY_PEAK_SENSOR)
            return await self.async_step_parameters()

        return self.async_show_form(step_id=STEP_ID_OPTIONAL, data_schema=schema)

    async def async_step_parameters(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """§5.4 — initial parameters."""
        defaults: dict[str, Any] = {
            "mode": const.DEFAULT_MODE,
            "min_soc": const.DEFAULT_MIN_SOC,
            "max_soc": const.DEFAULT_MAX_SOC,
            "max_battery_power": const.DEFAULT_MAX_BATTERY_POWER,
            "send_interval": const.DEFAULT_SEND_INTERVAL,
            "grid_smoothing": const.DEFAULT_GRID_SMOOTHING_WINDOW,
            "battery_smoothing": const.DEFAULT_BATTERY_SMOOTHING_WINDOW,
            "battery_capacity": const.DEFAULT_BATTERY_CAPACITY_WH,
            "evening_min_soc": const.DEFAULT_EVENING_MIN_SOC,
            "evening_max_charge_power": const.DEFAULT_EVENING_MAX_CHARGE_POWER,
            "capacity_tariff_enabled": const.DEFAULT_CAPACITY_TARIFF_ENABLED,
            "max_desired_peak": const.DEFAULT_MAX_DESIRED_PEAK_W,
            "manual_target_soc": const.DEFAULT_MANUAL_TARGET_SOC,
            "manual_power": const.DEFAULT_MANUAL_POWER,
            "evening_peak_time": "18:00",
            "passive_floor_time": "13:00",
        }

        if user_input is not None:
            merged = {**defaults, **user_input}
            errors: dict[str, str] = {}
            if merged["min_soc"] >= merged["max_soc"]:
                errors["base"] = "invalid_min_max_soc"
            elif merged["evening_max_charge_power"] > merged["max_battery_power"]:
                errors["base"] = "evening_charge_too_high"
            elif merged["manual_power"] > merged["max_battery_power"]:
                errors["base"] = "manual_power_too_high"
            if errors:
                return self.async_show_form(
                    step_id=STEP_ID_PARAMS,
                    data_schema=_param_schema_defaults(merged),
                    errors=errors,
                )

            data: dict[str, Any] = {
                const.CONF_USE_DISCOVERY: not self._use_manual,
                const.CONF_GRID_POWER: self._grid_entity,
                const.CONF_CAP_NOW_SENSOR: self._cap_sensor,
                const.CONF_MONTHLY_PEAK_SENSOR: self._monthly_sensor,
            }
            if self._use_manual:
                data[const.CONF_MANUAL_ENTITIES] = self._manual_entities
            else:
                data[const.CONF_MARSTEK_DEVICE_ID] = self._picked_device_id

            uid = (
                self._picked_device_id or self._manual_entities.get(
                    const.CONF_ENTITY_BATTERY_SOC, "manual"
                )
            )
            uid = f"{uid}_{self._grid_entity}"
            await self.async_set_unique_id(uid)
            self._abort_if_unique_id_configured()

            options = dict(user_input)
            return self.async_create_entry(
                title="Marstek Battery Controller",
                data=data,
                options=options,
            )

        return self.async_show_form(
            step_id=STEP_ID_PARAMS,
            data_schema=_param_schema_defaults(defaults),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """HA options menu."""
        return MarstekBatteryOptionsFlow(config_entry)


class MarstekBatteryOptionsFlow(OptionsFlow):
    """Edit integration options."""

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize."""
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Merge validated options back into the config entry."""
        defaults: dict[str, Any] = {
            "mode": const.DEFAULT_MODE,
            "min_soc": const.DEFAULT_MIN_SOC,
            "max_soc": const.DEFAULT_MAX_SOC,
            "max_battery_power": const.DEFAULT_MAX_BATTERY_POWER,
            "send_interval": const.DEFAULT_SEND_INTERVAL,
            "grid_smoothing": const.DEFAULT_GRID_SMOOTHING_WINDOW,
            "battery_smoothing": const.DEFAULT_BATTERY_SMOOTHING_WINDOW,
            "battery_capacity": const.DEFAULT_BATTERY_CAPACITY_WH,
            "evening_min_soc": const.DEFAULT_EVENING_MIN_SOC,
            "evening_max_charge_power": const.DEFAULT_EVENING_MAX_CHARGE_POWER,
            "capacity_tariff_enabled": const.DEFAULT_CAPACITY_TARIFF_ENABLED,
            "max_desired_peak": const.DEFAULT_MAX_DESIRED_PEAK_W,
            "manual_target_soc": const.DEFAULT_MANUAL_TARGET_SOC,
            "manual_power": const.DEFAULT_MANUAL_POWER,
            "evening_peak_time": "18:00",
            "passive_floor_time": "13:00",
        }
        merged_in = {**defaults, **self._entry.options}

        if user_input is not None:
            merged = {**merged_in, **user_input}
            if merged["min_soc"] >= merged["max_soc"]:
                return self.async_show_form(
                    step_id="init",
                    data_schema=_param_schema_defaults(merged),
                    errors={"base": "invalid_min_max_soc"},
                )
            if merged["evening_max_charge_power"] > merged["max_battery_power"]:
                return self.async_show_form(
                    step_id="init",
                    data_schema=_param_schema_defaults(merged),
                    errors={"base": "evening_charge_too_high"},
                )
            if merged["manual_power"] > merged["max_battery_power"]:
                return self.async_show_form(
                    step_id="init",
                    data_schema=_param_schema_defaults(merged),
                    errors={"base": "manual_power_too_high"},
                )
            return self.async_create_entry(title="", data=merged)

        return self.async_show_form(
            step_id="init",
            data_schema=_param_schema_defaults(merged_in),
        )

