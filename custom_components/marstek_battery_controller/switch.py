"""Capacity tariff enable switch."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
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
    """Set up switches."""
    coordinator: MarstekBatteryCoordinator = hass.data[const.DOMAIN][entry.entry_id]
    async_add_entities(
        [
            MarstekCapacityTariffSwitch(
                coordinator, entry.entry_id, marstek_controller_device_info(hass, entry)
            )
        ]
    )


class MarstekCapacityTariffSwitch(CoordinatorEntity[MarstekBatteryCoordinator], SwitchEntity):
    """§7 — Capacity tariff enabled."""

    _attr_has_entity_name = True
    _attr_translation_key = const.ENTITY_CAPACITY_TARIFF_ENABLED
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
        self._attr_unique_id = f"{entry_id}_{const.ENTITY_CAPACITY_TARIFF_ENABLED}"
        self._attr_device_info = device_info

    @property
    def is_on(self) -> bool | None:
        """Whether capacity-tariff logic is active."""
        return self.coordinator.capacity_tariff_enabled_flag

    async def async_turn_on(self, **kwargs: object) -> None:
        """Enable."""
        self.coordinator.set_capacity_tariff_enabled(True)
        self.coordinator.persist_entry_options(self.hass, self._entry_id)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        """Disable."""
        self.coordinator.set_capacity_tariff_enabled(False)
        self.coordinator.persist_entry_options(self.hass, self._entry_id)
        self.async_write_ha_state()
