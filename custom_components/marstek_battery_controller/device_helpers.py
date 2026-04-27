"""Registry device grouping for controller entities."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo

from . import const


def marstek_controller_device_info(hass: HomeAssistant, entry: ConfigEntry) -> DeviceInfo:
    """Expose one device per config entry so UI groups all entities together.

    When discovery provided a marstek_modbus device id, links via ``via_device`` so the
    controller appears under that hardware in the device tree.
    """
    via: tuple[str, str] | None = None
    if entry.data.get(const.CONF_USE_DISCOVERY):
        parent_registry_id = entry.data.get(const.CONF_MARSTEK_DEVICE_ID)
        if parent_registry_id:
            parent = dr.async_get(hass).async_get(parent_registry_id)
            if parent and parent.identifiers:
                via = next(iter(sorted(parent.identifiers)))
    return DeviceInfo(
        identifiers={(const.DOMAIN, entry.entry_id)},
        name=entry.title or "Marstek Battery Controller",
        manufacturer="plan-d-io",
        model="Battery Controller",
        via_device=via,
    )
