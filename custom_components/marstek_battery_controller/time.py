"""Time-of-day parameters (evening peak, passive floor start) — time-only UI."""

from __future__ import annotations

from datetime import time
import logging

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import const
from .coordinator import MarstekBatteryCoordinator
from .device_helpers import marstek_controller_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up time entities."""
    coordinator: MarstekBatteryCoordinator = hass.data[const.DOMAIN][entry.entry_id]
    dev_info = marstek_controller_device_info(hass, entry)
    async_add_entities(
        [
            MarstekEveningPeakTime(coordinator, entry.entry_id, dev_info),
            MarstekPassiveFloorTime(coordinator, entry.entry_id, dev_info),
        ]
    )


class MarstekEveningPeakTime(CoordinatorEntity[MarstekBatteryCoordinator], TimeEntity):
    """§7 — Evening peak start (wall-clock time only)."""

    _attr_has_entity_name = True
    _attr_translation_key = const.ENTITY_EVENING_PEAK_START
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: MarstekBatteryCoordinator,
        entry_id: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_{const.ENTITY_EVENING_PEAK_START}"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> time | None:
        """Configured peak time."""
        return self.coordinator.evening_peak_time_value

    async def async_set_value(self, value: time) -> None:
        """Persist new peak time."""
        self.coordinator.set_evening_peak_time(value)
        self.coordinator.persist_entry_options(self.hass, self._entry_id)
        self.async_write_ha_state()


class MarstekPassiveFloorTime(CoordinatorEntity[MarstekBatteryCoordinator], TimeEntity):
    """§7 — Passive floor-protection start (wall-clock time only)."""

    _attr_has_entity_name = True
    _attr_translation_key = const.ENTITY_PASSIVE_FLOOR_PROTECTION_START
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: MarstekBatteryCoordinator,
        entry_id: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_{const.ENTITY_PASSIVE_FLOOR_PROTECTION_START}"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> time | None:
        """Configured protection start time."""
        return self.coordinator.passive_floor_time_value

    async def async_set_value(self, value: time) -> None:
        """Persist new protection start time."""
        self.coordinator.set_passive_floor_time(value)
        self.coordinator.persist_entry_options(self.hass, self._entry_id)
        self.async_write_ha_state()
