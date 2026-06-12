"""Mode select entity."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
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
    """Set up mode select."""
    coordinator: MarstekBatteryCoordinator = hass.data[const.DOMAIN][entry.entry_id]
    async_add_entities(
        [MarstekModeSelect(coordinator, entry.entry_id, marstek_controller_device_info(hass, entry))]
    )


class MarstekModeSelect(CoordinatorEntity[MarstekBatteryCoordinator], SelectEntity):
    """Operating mode (§6)."""

    _attr_has_entity_name = True
    _attr_translation_key = const.ENTITY_MODE
    _attr_entity_category = None

    def __init__(
        self,
        coordinator: MarstekBatteryCoordinator,
        entry_id: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize mode select."""
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_{const.ENTITY_MODE}"
        self._attr_device_info = device_info
        self._attr_options = list(const.MODES)

    @property
    def current_option(self) -> str | None:
        """Selected mode."""
        return self.coordinator.current_mode

    async def async_select_option(self, option: str) -> None:
        """Handle mode transitions per §13."""
        if option not in const.MODES:
            return
        coord = self.coordinator
        old = coord.current_mode

        if option == const.MODE_MANUAL:
            if old != const.MODE_MANUAL:
                coord.previous_mode = old
                await coord.storage.async_save_previous_mode(old)
        elif old == const.MODE_MANUAL:
            coord.previous_mode = None
            await coord.storage.async_save_previous_mode(None)

        if option == const.MODE_RELEASED and old != const.MODE_RELEASED:
            coord.set_mode(const.MODE_RELEASED)
            coord.persist_entry_options(self.hass, self._entry_id)
            self.async_write_ha_state()
            self.hass.async_create_task(coord.async_schedule_released_cleanup())
            return

        coord.set_mode(option)
        coord.persist_entry_options(self.hass, self._entry_id)
        self.async_write_ha_state()
