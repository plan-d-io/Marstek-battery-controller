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
from .homewizard_discovery import HomeWizardCandidate, list_homewizard_p1_candidates, probe_homewizard
from .viperrnmc_options import (
    apply_high_interval,
    find_marstek_modbus_entry_for_device,
    get_high_interval,
)

_LOGGER = logging.getLogger(__name__)

STEP_ID_MANUAL = "manual_devices"
STEP_ID_GRID = "grid"
STEP_ID_OPTIONAL = "optional"
STEP_ID_PARAMS = "parameters"
STEP_ID_SCAN_INTERVAL_CONSENT = "scan_interval_consent"
STEP_ID_GRID_SOURCE = "grid_source"
STEP_ID_HOMEWIZARD_PICK = "homewizard_pick"
STEP_ID_HOMEWIZARD_MANUAL = "homewizard_manual"


def _power_sensor_selector_strict() -> selector.EntitySelector:
    """Grid power: any power-class sensor (P1, HomeWizard, etc.). No UoM filter — HA rejects it."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain="sensor",
            device_class=SensorDeviceClass.POWER,
        )
    )


def _power_sensor_selector_loose() -> selector.EntitySelector:
    """Fallback if strict filters fail validation or frontend serialization."""
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))


def _build_grid_power_schema(*, loose: bool) -> vol.Schema:
    """Build grid step schema; ``loose`` is domain=sensor only (no device_class)."""
    sel = _power_sensor_selector_loose() if loose else _power_sensor_selector_strict()
    return vol.Schema({vol.Required(const.CONF_GRID_POWER): sel})


def _repr_user_input(data: dict[str, Any] | None) -> str:
    """Safe single-line summary for logs (avoid secrets beyond entity ids)."""
    if data is None:
        return "None"
    parts: list[str] = []
    for key in sorted(data.keys()):
        val = data[key]
        if isinstance(val, dict):
            parts.append(f"{key}={{...{len(val)} keys}}")
        else:
            parts.append(f"{key}={val!r}")
    return ", ".join(parts)


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
    """Optional peak / average power sensors (same device_class as grid)."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain="sensor",
            device_class=SensorDeviceClass.POWER,
        )
    )


def _param_schema_defaults(
    merged: dict[str, Any], *, initial: bool = False
) -> vol.Schema:
    """Voluptuous schema with explicit defaults. Initial setup omits mode and manual fields."""
    d = merged
    schema_dict: dict[Any, Any] = {}

    if not initial:
        schema_dict[
            vol.Optional("mode", default=d.get("mode", const.DEFAULT_MODE))
        ] = vol.In(const.MODES)

    schema_dict[
        vol.Optional("min_soc", default=d.get("min_soc", const.DEFAULT_MIN_SOC))
    ] = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=const.MIN_SOC_PCT,
            max=const.MAX_SOC_PCT,
            step=1,
            mode=selector.NumberSelectorMode.SLIDER,
            unit_of_measurement="%",
        )
    )
    schema_dict[
        vol.Optional("max_soc", default=d.get("max_soc", const.DEFAULT_MAX_SOC))
    ] = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=const.MIN_SOC_PCT,
            max=const.MAX_SOC_PCT,
            step=1,
            mode=selector.NumberSelectorMode.SLIDER,
            unit_of_measurement="%",
        )
    )
    schema_dict[
        vol.Optional(
            "max_battery_power",
            default=d.get("max_battery_power", const.DEFAULT_MAX_BATTERY_POWER),
        )
    ] = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=const.MIN_BATTERY_POWER_W,
            max=const.MAX_BATTERY_POWER_LIMIT_W,
            step=50,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement=UnitOfPower.WATT,
        )
    )
    schema_dict[
        vol.Optional(
            "battery_capacity",
            default=d.get("battery_capacity", const.DEFAULT_BATTERY_CAPACITY_WH),
        )
    ] = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=const.MIN_BATTERY_CAPACITY_WH,
            max=const.MAX_BATTERY_CAPACITY_WH,
            step=100,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="Wh",
        )
    )
    schema_dict[
        vol.Optional(
            "capacity_tariff_enabled",
            default=d.get(
                "capacity_tariff_enabled", const.DEFAULT_CAPACITY_TARIFF_ENABLED
            ),
        )
    ] = selector.BooleanSelector()
    schema_dict[
        vol.Optional(
            "send_interval",
            default=d.get("send_interval", const.DEFAULT_SEND_INTERVAL),
        )
    ] = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=const.MIN_SEND_INTERVAL_S,
            max=const.MAX_SEND_INTERVAL_S,
            step=1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    )
    schema_dict[
        vol.Optional(
            "grid_smoothing",
            default=d.get("grid_smoothing", const.DEFAULT_GRID_SMOOTHING_WINDOW),
        )
    ] = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=const.MIN_SMOOTHING_WINDOW_S,
            max=const.MAX_SMOOTHING_WINDOW_S,
            step=1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    )
    schema_dict[
        vol.Optional(
            "battery_smoothing",
            default=d.get(
                "battery_smoothing", const.DEFAULT_BATTERY_SMOOTHING_WINDOW
            ),
        )
    ] = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=const.MIN_SMOOTHING_WINDOW_S,
            max=const.MAX_SMOOTHING_WINDOW_S,
            step=1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    )
    schema_dict[
        vol.Optional(
            "evening_min_soc",
            default=d.get("evening_min_soc", const.DEFAULT_EVENING_MIN_SOC),
        )
    ] = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=const.MIN_SOC_PCT,
            max=const.MAX_SOC_PCT,
            step=1,
            mode=selector.NumberSelectorMode.SLIDER,
            unit_of_measurement="%",
        )
    )
    schema_dict[
        vol.Optional(
            "evening_max_charge_power",
            default=d.get(
                "evening_max_charge_power", const.DEFAULT_EVENING_MAX_CHARGE_POWER
            ),
        )
    ] = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=const.MIN_EVENING_CHARGE_POWER_W,
            max=const.MAX_BATTERY_POWER_LIMIT_W,
            step=50,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement=UnitOfPower.WATT,
        )
    )
    schema_dict[
        vol.Optional(
            "max_desired_peak",
            default=d.get("max_desired_peak", const.DEFAULT_MAX_DESIRED_PEAK_W),
        )
    ] = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=const.MIN_MAX_DESIRED_PEAK_W,
            max=const.MAX_MAX_DESIRED_PEAK_W,
            step=50,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement=UnitOfPower.WATT,
        )
    )

    if not initial:
        schema_dict[
            vol.Optional(
                "manual_target_soc",
                default=d.get("manual_target_soc", const.DEFAULT_MANUAL_TARGET_SOC),
            )
        ] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=const.MIN_SOC_PCT,
                max=const.MAX_SOC_PCT,
                step=1,
                mode=selector.NumberSelectorMode.SLIDER,
                unit_of_measurement="%",
            )
        )
        schema_dict[
            vol.Optional(
                "manual_power",
                default=d.get("manual_power", const.DEFAULT_MANUAL_POWER),
            )
        ] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=const.MIN_BATTERY_POWER_W,
                max=const.MAX_BATTERY_POWER_LIMIT_W,
                step=50,
                mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement=UnitOfPower.WATT,
            )
        )

    schema_dict[
        vol.Optional(
            "evening_peak_time",
            default=d.get("evening_peak_time", "18:00"),
        )
    ] = selector.TextSelector()
    schema_dict[
        vol.Optional(
            "passive_floor_time",
            default=d.get("passive_floor_time", "13:00"),
        )
    ] = selector.TextSelector()

    return vol.Schema(schema_dict)


class MarstekBatteryConfigFlow(ConfigFlow, domain=const.DOMAIN):
    """Handle UI configuration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow."""
        self._picked_device_id: str | None = None
        self._use_manual: bool = False
        self._manual_entities: dict[str, str] = {}
        self._grid_entity: str = ""
        self._grid_source: str = const.GRID_SOURCE_EXISTING_SENSOR
        self._homewizard_ip: str | None = None
        self._homewizard_candidates: list[HomeWizardCandidate] = []
        self._marstek_modbus_entry_id: str | None = None
        self._scan_interval_offered: bool = False
        self._cap_sensor: str | None = None
        self._monthly_sensor: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """§5.1 — device or manual."""
        devices = list_marstek_devices(self.hass)
        _LOGGER.info(
            "%s async_step_user: device_ids=%s submit=%s",
            const.DOMAIN,
            [pair[0] for pair in devices],
            _repr_user_input(user_input),
        )

        if user_input is not None:
            if _manual_toggle_is_on(user_input.get("manual")):
                self._use_manual = True
                self._picked_device_id = None
                _LOGGER.info("%s manual setup branch → manual_devices step", const.DOMAIN)
                return await self.async_step_manual_devices()
            device_id = user_input.get(const.CONF_MARSTEK_DEVICE_ID)
            if not device_id:
                _LOGGER.warning("%s submit without device_id", const.DOMAIN)
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
                _LOGGER.warning(
                    "%s role discovery returned None for device_id=%s",
                    const.DOMAIN,
                    device_id,
                )
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._schema_user_pick(devices),
                    errors={"base": "discovery_failed"},
                )
            self._picked_device_id = str(device_id)
            self._use_manual = False
            _LOGGER.info(
                "%s discovery OK device_id=%s → scan-interval consent step",
                const.DOMAIN,
                self._picked_device_id,
            )
            try:
                return await self.async_step_scan_interval_consent()
            except Exception:
                _LOGGER.exception(
                    "%s scan/grid-source step crashed after device pick device_id=%s",
                    const.DOMAIN,
                    device_id,
                )
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._schema_user_pick(devices),
                    errors={"base": "internal_error"},
                )

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
            return await self.async_step_grid_source()

        return self.async_show_form(step_id=STEP_ID_MANUAL, data_schema=schema)

    async def async_step_scan_interval_consent(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Offer to set marstek_modbus high-priority polling to 1 s."""
        if self._use_manual or not self._picked_device_id:
            return await self.async_step_grid_source()

        entry = find_marstek_modbus_entry_for_device(self.hass, self._picked_device_id)
        if entry is None:
            _LOGGER.warning(
                "Could not find marstek_modbus entry for device_id=%s; skipping consent step",
                self._picked_device_id,
            )
            return await self.async_step_grid_source()

        self._marstek_modbus_entry_id = entry.entry_id
        current = get_high_interval(entry)
        if current <= const.VIPER_HIGH_INTERVAL_RECOMMENDED:
            return await self.async_step_grid_source()

        if user_input is not None:
            self._scan_interval_offered = True
            if user_input.get("apply"):
                try:
                    await apply_high_interval(
                        self.hass, entry, const.VIPER_HIGH_INTERVAL_RECOMMENDED
                    )
                except Exception:
                    _LOGGER.exception(
                        "Failed to update/reload marstek_modbus high interval for entry_id=%s",
                        entry.entry_id,
                    )
            return await self.async_step_grid_source()

        schema = vol.Schema(
            {vol.Required("apply", default=True): selector.BooleanSelector()}
        )
        return self.async_show_form(
            step_id=STEP_ID_SCAN_INTERVAL_CONSENT,
            data_schema=schema,
            description_placeholders={
                "current": str(current),
                "recommended": str(const.VIPER_HIGH_INTERVAL_RECOMMENDED),
            },
        )

    async def async_step_grid_source(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose how to source grid power."""
        candidates = await list_homewizard_p1_candidates(self.hass)
        self._homewizard_candidates = candidates

        if user_input is not None:
            choice = str(user_input["grid_source_type"])
            if choice == const.GRID_SOURCE_EXISTING_SENSOR:
                self._grid_source = const.GRID_SOURCE_EXISTING_SENSOR
                return await self.async_step_grid()
            if choice == const.GRID_SOURCE_HOMEWIZARD_FAST_POLL:
                self._grid_source = const.GRID_SOURCE_HOMEWIZARD_FAST_POLL
                if len(candidates) == 1:
                    self._homewizard_ip = candidates[0].ip
                    return await self.async_step_optional()
                return await self.async_step_homewizard_pick()
            return await self.async_step_homewizard_manual()

        default = (
            const.GRID_SOURCE_HOMEWIZARD_FAST_POLL
            if candidates
            else const.GRID_SOURCE_EXISTING_SENSOR
        )
        options: list[dict[str, str]] = [
            {
                "value": const.GRID_SOURCE_EXISTING_SENSOR,
                "label": "Use an existing power sensor",
            }
        ]
        if candidates:
            if len(candidates) == 1:
                cand = candidates[0]
                label = (
                    "Fast-poll my HomeWizard P1 "
                    f"(detected: {cand.title} at {cand.ip}) — recommended"
                )
            else:
                label = f"Fast-poll my HomeWizard P1 ({len(candidates)} detected) — recommended"
            options.append(
                {
                    "value": const.GRID_SOURCE_HOMEWIZARD_FAST_POLL,
                    "label": label,
                }
            )
        options.append(
            {
                "value": "homewizard_manual",
                "label": "Manually enter HomeWizard P1 IP address",
            }
        )
        schema = vol.Schema(
            {
                vol.Required("grid_source_type", default=default): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(step_id=STEP_ID_GRID_SOURCE, data_schema=schema)

    async def async_step_homewizard_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick from multiple detected HomeWizard P1 devices."""
        if user_input is not None:
            self._homewizard_ip = user_input[const.CONF_HOMEWIZARD_IP]
            return await self.async_step_optional()

        options = [
            {"value": cand.ip, "label": f"{cand.title} ({cand.ip})"}
            for cand in self._homewizard_candidates
        ]
        schema = vol.Schema(
            {
                vol.Required(const.CONF_HOMEWIZARD_IP): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id=STEP_ID_HOMEWIZARD_PICK, data_schema=schema)

    async def async_step_homewizard_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manual IP entry for HomeWizard P1."""
        errors: dict[str, str] = {}

        if user_input is not None:
            ip = str(user_input[const.CONF_HOMEWIZARD_IP]).strip()
            cand = await probe_homewizard(self.hass, ip)
            if cand is None:
                errors["base"] = "homewizard_unreachable"
            elif cand.product_type != const.HOMEWIZARD_PRODUCT_P1:
                errors["base"] = "homewizard_wrong_product"
            else:
                self._grid_source = const.GRID_SOURCE_HOMEWIZARD_FAST_POLL
                self._homewizard_ip = ip
                return await self.async_step_optional()

        default_ip = (
            str(user_input.get(const.CONF_HOMEWIZARD_IP, ""))
            if user_input is not None
            else ""
        )
        schema = vol.Schema(
            {vol.Required(const.CONF_HOMEWIZARD_IP, default=default_ip): str}
        )
        return self.async_show_form(
            step_id=STEP_ID_HOMEWIZARD_MANUAL,
            data_schema=schema,
            errors=errors,
        )

    async def async_step_grid(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """§5.2 — grid sensor."""
        _LOGGER.info(
            "%s async_step_grid enter user_input=%s",
            const.DOMAIN,
            _repr_user_input(user_input),
        )

        if user_input is not None:
            try:
                self._grid_entity = user_input[const.CONF_GRID_POWER]
            except KeyError:
                _LOGGER.exception(
                    "%s grid step missing key %s in %s",
                    const.DOMAIN,
                    const.CONF_GRID_POWER,
                    list(user_input.keys()),
                )
                raise
            _LOGGER.info("%s grid entity picked: %s", const.DOMAIN, self._grid_entity)
            return await self.async_step_optional()

        try:
            schema = _build_grid_power_schema(loose=False)
            _LOGGER.debug("%s built strict grid power schema OK", const.DOMAIN)
        except Exception:
            _LOGGER.exception("%s strict grid schema build failed — trying loose picker", const.DOMAIN)
            schema = _build_grid_power_schema(loose=True)

        try:
            return self.async_show_form(step_id=STEP_ID_GRID, data_schema=schema)
        except Exception:
            _LOGGER.exception("%s async_show_form(grid) failed with strict schema — retry loose", const.DOMAIN)
            schema = _build_grid_power_schema(loose=True)
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
            if errors:
                return self.async_show_form(
                    step_id=STEP_ID_PARAMS,
                    data_schema=_param_schema_defaults(merged, initial=True),
                    errors=errors,
                )

            data: dict[str, Any] = {
                const.CONF_USE_DISCOVERY: not self._use_manual,
                const.CONF_GRID_SOURCE_TYPE: self._grid_source,
                const.CONF_CAP_NOW_SENSOR: self._cap_sensor,
                const.CONF_MONTHLY_PEAK_SENSOR: self._monthly_sensor,
            }
            if self._grid_source == const.GRID_SOURCE_EXISTING_SENSOR:
                data[const.CONF_GRID_POWER] = self._grid_entity
            else:
                data[const.CONF_HOMEWIZARD_IP] = self._homewizard_ip
            if self._use_manual:
                data[const.CONF_MANUAL_ENTITIES] = self._manual_entities
            else:
                data[const.CONF_MARSTEK_DEVICE_ID] = self._picked_device_id

            uid = (
                self._picked_device_id or self._manual_entities.get(
                    const.CONF_ENTITY_BATTERY_SOC, "manual"
                )
            )
            if self._grid_source == const.GRID_SOURCE_EXISTING_SENSOR:
                grid_uid = self._grid_entity
            else:
                grid_uid = f"hw_{self._homewizard_ip}"
            uid = f"{uid}_{grid_uid}"
            await self.async_set_unique_id(uid)
            self._abort_if_unique_id_configured()

            options = merged
            return self.async_create_entry(
                title="Marstek Battery Controller",
                data=data,
                options=options,
            )

        return self.async_show_form(
            step_id=STEP_ID_PARAMS,
            data_schema=_param_schema_defaults(defaults, initial=True),
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
                    data_schema=_param_schema_defaults(merged, initial=False),
                    errors={"base": "invalid_min_max_soc"},
                )
            if merged["evening_max_charge_power"] > merged["max_battery_power"]:
                return self.async_show_form(
                    step_id="init",
                    data_schema=_param_schema_defaults(merged, initial=False),
                    errors={"base": "evening_charge_too_high"},
                )
            if merged["manual_power"] > merged["max_battery_power"]:
                return self.async_show_form(
                    step_id="init",
                    data_schema=_param_schema_defaults(merged, initial=False),
                    errors={"base": "manual_power_too_high"},
                )
            return self.async_create_entry(title="", data=merged)

        return self.async_show_form(
            step_id="init",
            data_schema=_param_schema_defaults(merged_in, initial=False),
        )

