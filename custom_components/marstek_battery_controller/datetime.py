"""Time-of-day parameters (evening peak, passive floor start)."""

from __future__ import annotations

from datetime import datetime
import logging

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import const
from .coordinator import MarstekBatteryCoordinator
from .device_helpers import marstek_controller_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up datetime entities."""
    coordinator: MarstekBatteryCoordinator = hass.data[const.DOMAIN][entry.entry_id]
    dev_info = marstek_controller_device_info(hass, entry)
    async_add_entities(
        [
            MarstekEveningPeakDatetime(coordinator, entry.entry_id, dev_info),
            MarstekPassiveFloorDatetime(coordinator, entry.entry_id, dev_info),
        ]
    )


class MarstekEveningPeakDatetime(CoordinatorEntity[MarstekBatteryCoordinator], DateTimeEntity):
    """§7 — Evening peak start (time)."""

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
    def native_value(self) -> datetime | None:
        """Today's date with configured peak time."""
        now = dt_util.now()
        t = self.coordinator.evening_peak_time_value
        return datetime.combine(now.date(), t, tzinfo=now.tzinfo)

    async def async_set_value(self, value: datetime) -> None:
        """Persist new peak time."""
        self.coordinator.set_evening_peak_time(value)
        self.coordinator.persist_entry_options(self.hass, self._entry_id)
        self.async_write_ha_state()


class MarstekPassiveFloorDatetime(CoordinatorEntity[MarstekBatteryCoordinator], DateTimeEntity):
    """§7 — Passive floor-protection start (time)."""

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
    def native_value(self) -> datetime | None:
        """Today's date with configured protection start time."""
        now = dt_util.now()
        t = self.coordinator.passive_floor_time_value
        return datetime.combine(now.date(), t, tzinfo=now.tzinfo)

    async def async_set_value(self, value: datetime) -> None:
        """Persist new protection start time."""
        self.coordinator.set_passive_floor_time(value)
        self.coordinator.persist_entry_options(self.hass, self._entry_id)
        self.async_write_ha_state()
