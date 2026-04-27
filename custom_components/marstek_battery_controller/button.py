"""Manual trigger button."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import const
from .coordinator import MarstekBatteryCoordinator, parse_optional_float_state

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button."""
    coordinator: MarstekBatteryCoordinator = hass.data[const.DOMAIN][entry.entry_id]
    async_add_entities([MarstekManualTriggerButton(coordinator, entry.entry_id)])


class MarstekManualTriggerButton(CoordinatorEntity[MarstekBatteryCoordinator], ButtonEntity):
    """§7.2 — Manual trigger."""

    _attr_has_entity_name = True
    _attr_translation_key = const.ENTITY_MANUAL_TRIGGER

    def __init__(self, coordinator: MarstekBatteryCoordinator, entry_id: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_{const.ENTITY_MANUAL_TRIGGER}"

    async def async_press(self) -> None:
        """Validate and enter manual mode."""
        coord = self.coordinator
        soc_st = self.hass.states.get(coord.runtime.resolved.battery_soc)
        cur = parse_optional_float_state(soc_st)
        if cur is None:
            _LOGGER.warning("Manual trigger: SoC unavailable")
            return
        if round(cur) == round(coord._manual_target_soc):  # noqa: SLF001
            _LOGGER.debug("Manual trigger: target equals SoC; no action")
            return

        old = coord.current_mode
        if old != const.MODE_MANUAL:
            coord.previous_mode = old
            await coord.storage.async_save_previous_mode(old)
        coord.set_mode(const.MODE_MANUAL)
        coord.persist_entry_options(self.hass, self._entry_id)
