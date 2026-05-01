"""Diagnostic sensors (§14)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import const
from .coordinator import MarstekBatteryCoordinator
from .device_helpers import marstek_controller_device_info

_LOGGER = logging.getLogger(__name__)


def _latest_start_charge_fn(c: MarstekBatteryCoordinator) -> datetime | None:
    v = c.snapshot_latest_start_charge
    if v is None or v == const.LATEST_START_NO_NEED:
        return None
    return v


@dataclass(frozen=True, kw_only=True)
class MarstekDiagDescription(SensorEntityDescription):
    """Diagnostic row from §14."""

    value_fn: Callable[[MarstekBatteryCoordinator], StateType | datetime | None]
    enum_options: tuple[str, ...] | None = None
    registry_enabled_default: bool | None = None


def _all_descriptions(
    include_internal_cap: bool,
) -> tuple[MarstekDiagDescription, ...]:
    """Build sensors for this config entry."""
    main_sensors: list[MarstekDiagDescription] = [
        MarstekDiagDescription(
            key=const.SENSOR_OPERATING_STATE,
            translation_key=const.SENSOR_OPERATING_STATE,
            entity_category=None,
            enum_options=const.OPERATING_STATE_OPTIONS,
            value_fn=lambda c: c.last_calc_output.operating_state
            if c.last_calc_output
            else None,
        ),
        MarstekDiagDescription(
            key=const.SENSOR_LAST_SENT_SETPOINT,
            translation_key=const.SENSOR_LAST_SENT_SETPOINT,
            native_unit_of_measurement=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            entity_category=None,
            value_fn=lambda c: c.last_sent_setpoint,
        ),
    ]

    diagnostic_core: list[MarstekDiagDescription] = [
        MarstekDiagDescription(
            key=const.SENSOR_TARGET_SETPOINT,
            translation_key=const.SENSOR_TARGET_SETPOINT,
            native_unit_of_measurement=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda c: c.target_setpoint,
        ),
        MarstekDiagDescription(
            key=const.SENSOR_REASON_CODE,
            translation_key=const.SENSOR_REASON_CODE,
            entity_category=EntityCategory.DIAGNOSTIC,
            enum_options=const.REASON_CODE_OPTIONS,
            value_fn=lambda c: c.last_calc_output.reason_code
            if c.last_calc_output
            else None,
        ),
        MarstekDiagDescription(
            key=const.SENSOR_LATEST_START_CHARGE,
            translation_key=const.SENSOR_LATEST_START_CHARGE,
            device_class=SensorDeviceClass.TIMESTAMP,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=_latest_start_charge_fn,
        ),
        MarstekDiagDescription(
            key=const.SENSOR_EFFECTIVE_CAP_THRESHOLD,
            translation_key=const.SENSOR_EFFECTIVE_CAP_THRESHOLD,
            native_unit_of_measurement=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda c: c.effective_cap_w(),
        ),
        MarstekDiagDescription(
            key=const.SENSOR_GRID_POWER_SMOOTHED,
            translation_key=const.SENSOR_GRID_POWER_SMOOTHED,
            native_unit_of_measurement=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda c: c.snapshot_grid_smoothed,
        ),
        MarstekDiagDescription(
            key=const.SENSOR_BATTERY_POWER_SMOOTHED,
            translation_key=const.SENSOR_BATTERY_POWER_SMOOTHED,
            native_unit_of_measurement=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda c: c.snapshot_batt_smoothed,
        ),
    ]

    if include_internal_cap:
        diagnostic_core.append(
            MarstekDiagDescription(
                key=const.SENSOR_CAP_NOW_INTERNAL,
                translation_key=const.SENSOR_CAP_NOW_INTERNAL,
                native_unit_of_measurement=UnitOfPower.WATT,
                device_class=SensorDeviceClass.POWER,
                entity_category=EntityCategory.DIAGNOSTIC,
                registry_enabled_default=False,
                value_fn=lambda c: c.snapshot_cap_now,
            )
        )

    diagnostic_tail: list[MarstekDiagDescription] = [
        MarstekDiagDescription(
            key=const.SENSOR_MINUTES_TO_PEAK_WINDOW,
            translation_key=const.SENSOR_MINUTES_TO_PEAK_WINDOW,
            native_unit_of_measurement=UnitOfTime.MINUTES,
            entity_category=EntityCategory.DIAGNOSTIC,
            suggested_display_precision=1,
            value_fn=lambda c: c.diagnostic_minutes_to_peak(),
        ),
        MarstekDiagDescription(
            key=const.SENSOR_ENERGY_NEEDED_FOR_RESERVE,
            translation_key=const.SENSOR_ENERGY_NEEDED_FOR_RESERVE,
            native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda c: c.diagnostic_energy_needed_reserve_wh(),
        ),
    ]

    return tuple(main_sensors + diagnostic_core + diagnostic_tail)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create sensors."""
    coordinator: MarstekBatteryCoordinator = hass.data[const.DOMAIN][entry.entry_id]
    dev_info = marstek_controller_device_info(hass, entry)
    entities = [
        MarstekDiagnosticSensor(coordinator, entry.entry_id, d, dev_info)
        for d in _all_descriptions(coordinator.runtime.use_internal_cap_now)
    ]
    if coordinator.runtime.grid_power_poller is not None:
        entities.append(HomeWizardGridPowerSensor(coordinator, entry.entry_id, dev_info))
    async_add_entities(entities)


class MarstekDiagnosticSensor(
    CoordinatorEntity[MarstekBatteryCoordinator],
    SensorEntity,
):
    """Read-only sensor value from coordinator."""

    entity_description: MarstekDiagDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MarstekBatteryCoordinator,
        entry_id: str,
        description: MarstekDiagDescription,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = device_info

        if description.enum_options is not None:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(description.enum_options)
        if description.registry_enabled_default is not None:
            self._attr_entity_registry_enabled_default = description.registry_enabled_default

    @property
    def native_value(self) -> StateType | datetime | None:
        """Poll coordinator snapshot."""
        try:
            return self.entity_description.value_fn(self.coordinator)
        except Exception as err:
            _LOGGER.debug(
                "Sensor read failed for %s: %s",
                self.entity_description.key,
                err,
            )
            return None


class HomeWizardGridPowerSensor(
    CoordinatorEntity[MarstekBatteryCoordinator],
    SensorEntity,
):
    """Diagnostic sensor exposing HomeWizard fast-poll value."""

    _attr_has_entity_name = True
    _attr_translation_key = const.SENSOR_GRID_POWER_FAST
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: MarstekBatteryCoordinator,
        entry_id: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_{const.SENSOR_GRID_POWER_FAST}"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> float | None:
        poller = self.coordinator.runtime.grid_power_poller
        if poller is None or not poller.is_fresh():
            return None
        return poller.latest_w

    @property
    def available(self) -> bool:
        poller = self.coordinator.runtime.grid_power_poller
        return poller is not None and poller.is_fresh()
