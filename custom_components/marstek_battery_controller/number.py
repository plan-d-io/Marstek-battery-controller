"""Numeric parameter entities (§7)."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Callable

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.helpers.entity import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import const
from .coordinator import MarstekBatteryCoordinator
from .device_helpers import marstek_controller_device_info

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class MarstekNumberEntityDescription(NumberEntityDescription):
    """Description with coordinator getter/setter names."""

    coord_get: Callable[[MarstekBatteryCoordinator], float]
    coord_set: Callable[[MarstekBatteryCoordinator, float], None]


def _descriptions() -> tuple[MarstekNumberEntityDescription, ...]:
    """Build entity descriptions."""
    return (
        MarstekNumberEntityDescription(
            key=const.ENTITY_MIN_SOC,
            translation_key=const.ENTITY_MIN_SOC,
            entity_category=EntityCategory.CONFIG,
            native_unit_of_measurement=PERCENTAGE,
            native_min_value=const.MIN_SOC_PCT,
            native_max_value=const.MAX_SOC_PCT,
            native_step=1,
            mode="slider",
            coord_get=lambda c: c.min_soc_value,
            coord_set=lambda c, v: c.set_min_soc(v),
        ),
        MarstekNumberEntityDescription(
            key=const.ENTITY_MAX_SOC,
            translation_key=const.ENTITY_MAX_SOC,
            entity_category=EntityCategory.CONFIG,
            native_unit_of_measurement=PERCENTAGE,
            native_min_value=const.MIN_SOC_PCT,
            native_max_value=const.MAX_SOC_PCT,
            native_step=1,
            mode="slider",
            coord_get=lambda c: c.max_soc_value,
            coord_set=lambda c, v: c.set_max_soc(v),
        ),
        MarstekNumberEntityDescription(
            key=const.ENTITY_MAX_BATTERY_POWER,
            translation_key=const.ENTITY_MAX_BATTERY_POWER,
            entity_category=EntityCategory.CONFIG,
            native_unit_of_measurement=UnitOfPower.WATT,
            native_min_value=const.MIN_BATTERY_POWER_W,
            native_max_value=const.MAX_BATTERY_POWER_LIMIT_W,
            native_step=50,
            mode="box",
            coord_get=lambda c: c.max_battery_power_value,
            coord_set=lambda c, v: c.set_max_battery_power(v),
        ),
        MarstekNumberEntityDescription(
            key=const.ENTITY_SEND_INTERVAL,
            translation_key=const.ENTITY_SEND_INTERVAL,
            entity_category=EntityCategory.CONFIG,
            native_unit_of_measurement="s",
            native_min_value=const.MIN_SEND_INTERVAL_S,
            native_max_value=const.MAX_SEND_INTERVAL_S,
            native_step=1,
            mode="box",
            coord_get=lambda c: c.send_interval_value,
            coord_set=lambda c, v: c.set_send_interval(v),
        ),
        MarstekNumberEntityDescription(
            key=const.ENTITY_GRID_SMOOTHING_WINDOW,
            translation_key=const.ENTITY_GRID_SMOOTHING_WINDOW,
            entity_category=EntityCategory.CONFIG,
            native_unit_of_measurement="s",
            native_min_value=const.MIN_SMOOTHING_WINDOW_S,
            native_max_value=const.MAX_SMOOTHING_WINDOW_S,
            native_step=1,
            mode="box",
            coord_get=lambda c: c.grid_smoothing_window_value,
            coord_set=lambda c, v: c.set_grid_smoothing_window(v),
        ),
        MarstekNumberEntityDescription(
            key=const.ENTITY_BATTERY_SMOOTHING_WINDOW,
            translation_key=const.ENTITY_BATTERY_SMOOTHING_WINDOW,
            entity_category=EntityCategory.CONFIG,
            native_unit_of_measurement="s",
            native_min_value=const.MIN_SMOOTHING_WINDOW_S,
            native_max_value=const.MAX_SMOOTHING_WINDOW_S,
            native_step=1,
            mode="box",
            coord_get=lambda c: c.battery_smoothing_window_value,
            coord_set=lambda c, v: c.set_battery_smoothing_window(v),
        ),
        MarstekNumberEntityDescription(
            key=const.ENTITY_BATTERY_CAPACITY,
            translation_key=const.ENTITY_BATTERY_CAPACITY,
            entity_category=EntityCategory.CONFIG,
            native_unit_of_measurement="Wh",
            native_min_value=const.MIN_BATTERY_CAPACITY_WH,
            native_max_value=const.MAX_BATTERY_CAPACITY_WH,
            native_step=100,
            mode="box",
            coord_get=lambda c: c.battery_capacity_wh_value,
            coord_set=lambda c, v: c.set_battery_capacity_wh(v),
        ),
        MarstekNumberEntityDescription(
            key=const.ENTITY_EVENING_MIN_SOC,
            translation_key=const.ENTITY_EVENING_MIN_SOC,
            entity_category=EntityCategory.CONFIG,
            native_unit_of_measurement=PERCENTAGE,
            native_min_value=const.MIN_SOC_PCT,
            native_max_value=const.MAX_SOC_PCT,
            native_step=1,
            mode="slider",
            coord_get=lambda c: c.evening_min_soc_value,
            coord_set=lambda c, v: c.set_evening_min_soc(v),
        ),
        MarstekNumberEntityDescription(
            key=const.ENTITY_EVENING_MAX_CHARGE_POWER,
            translation_key=const.ENTITY_EVENING_MAX_CHARGE_POWER,
            entity_category=EntityCategory.CONFIG,
            native_unit_of_measurement=UnitOfPower.WATT,
            native_min_value=const.MIN_EVENING_CHARGE_POWER_W,
            native_max_value=const.MAX_BATTERY_POWER_LIMIT_W,
            native_step=50,
            mode="box",
            coord_get=lambda c: c.evening_max_charge_power_value,
            coord_set=lambda c, v: c.set_evening_max_charge_power(v),
        ),
        MarstekNumberEntityDescription(
            key=const.ENTITY_MAX_DESIRED_PEAK,
            translation_key=const.ENTITY_MAX_DESIRED_PEAK,
            native_unit_of_measurement=UnitOfPower.WATT,
            native_min_value=const.MIN_MAX_DESIRED_PEAK_W,
            native_max_value=const.MAX_MAX_DESIRED_PEAK_W,
            native_step=50,
            mode="box",
            coord_get=lambda c: c.max_desired_peak_value,
            coord_set=lambda c, v: c.set_max_desired_peak(v),
        ),
        MarstekNumberEntityDescription(
            key=const.ENTITY_MANUAL_TARGET_SOC,
            translation_key=const.ENTITY_MANUAL_TARGET_SOC,
            native_unit_of_measurement=PERCENTAGE,
            native_min_value=const.MIN_SOC_PCT,
            native_max_value=const.MAX_SOC_PCT,
            native_step=1,
            mode="slider",
            coord_get=lambda c: c.manual_target_soc_value,
            coord_set=lambda c, v: c.set_manual_target_soc(v),
        ),
        MarstekNumberEntityDescription(
            key=const.ENTITY_MANUAL_POWER,
            translation_key=const.ENTITY_MANUAL_POWER,
            native_unit_of_measurement=UnitOfPower.WATT,
            native_min_value=const.MIN_BATTERY_POWER_W,
            native_max_value=const.MAX_BATTERY_POWER_LIMIT_W,
            native_step=50,
            mode="box",
            coord_get=lambda c: c.manual_power_value,
            coord_set=lambda c, v: c.set_manual_power(v),
        ),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities."""
    coordinator: MarstekBatteryCoordinator = hass.data[const.DOMAIN][entry.entry_id]
    dev_info = marstek_controller_device_info(hass, entry)
    entities = [
        MarstekCoordinatorNumber(coordinator, entry.entry_id, desc, dev_info)
        for desc in _descriptions()
    ]
    async_add_entities(entities)


class MarstekCoordinatorNumber(
    CoordinatorEntity[MarstekBatteryCoordinator],
    NumberEntity,
):
    """Single §7 numeric parameter."""

    entity_description: MarstekNumberEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MarstekBatteryCoordinator,
        entry_id: str,
        description: MarstekNumberEntityDescription,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> float | None:
        """Current value from coordinator."""
        return self.entity_description.coord_get(self.coordinator)

    async def async_set_native_value(self, value: float) -> None:
        """Validate §7.1 and apply."""
        desc = self.entity_description
        if desc.key in (const.ENTITY_MIN_SOC, const.ENTITY_MAX_SOC):
            other = (
                self.coordinator.max_soc_value
                if desc.key == const.ENTITY_MIN_SOC
                else self.coordinator.min_soc_value
            )
            if desc.key == const.ENTITY_MIN_SOC and value >= other:
                _LOGGER.warning("min_soc must be < max_soc")
                return
            if desc.key == const.ENTITY_MAX_SOC and value <= other:
                _LOGGER.warning("max_soc must be > min_soc")
                return
        if desc.key == const.ENTITY_EVENING_MAX_CHARGE_POWER:
            if value > self.coordinator.max_battery_power_value:
                _LOGGER.warning("evening_max_charge_power exceeds max_battery_power")
                return
        if desc.key == const.ENTITY_MANUAL_POWER:
            if value > self.coordinator.max_battery_power_value:
                _LOGGER.warning("manual_power exceeds max_battery_power")
                return

        desc.coord_set(self.coordinator, float(value))
        self.coordinator.persist_entry_options(self.hass, self._entry_id)
        self.async_write_ha_state()
