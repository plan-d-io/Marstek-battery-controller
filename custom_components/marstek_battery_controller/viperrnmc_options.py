"""Helpers to inspect/update ViperRNMC marstek_modbus polling options."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from . import const


def find_marstek_modbus_entry_for_device(
    hass: HomeAssistant, device_id: str
) -> ConfigEntry | None:
    """Return the marstek_modbus ConfigEntry that owns the given device_id, or None."""
    dev = dr.async_get(hass).async_get(device_id)
    if dev is None:
        return None
    for entry_id in dev.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None and entry.domain == const.VIPER_DOMAIN:
            return entry
    return None


def get_high_interval(entry: ConfigEntry) -> int:
    """Read current high polling interval with options→data→default fallback chain."""
    raw = (
        entry.options.get(const.VIPER_HIGH_INTERVAL_KEY)
        if entry.options
        else entry.data.get(const.VIPER_HIGH_INTERVAL_KEY)
    )
    if raw is None:
        raw = const.VIPER_HIGH_INTERVAL_DEFAULT_FALLBACK
    try:
        return int(raw)
    except (TypeError, ValueError):
        return const.VIPER_HIGH_INTERVAL_DEFAULT_FALLBACK


async def apply_high_interval(
    hass: HomeAssistant, entry: ConfigEntry, value: int
) -> None:
    """Update entry.options['high'] and reload entry. Awaits reload completion."""
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, const.VIPER_HIGH_INTERVAL_KEY: int(value)},
    )
    await hass.config_entries.async_reload(entry.entry_id)
