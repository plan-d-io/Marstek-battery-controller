"""Marstek Battery Controller custom integration."""

from __future__ import annotations

import logging
from datetime import time
from typing import TYPE_CHECKING, Any

from . import const

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["select", "number", "switch", "time", "button", "sensor"]

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


def _apply_options(coordinator: Any, options: dict[str, Any]) -> None:
    """Hydrate coordinator from config entry options dict."""
    if not options:
        return
    if "mode" in options:
        coordinator.set_mode(options["mode"])
    if "min_soc" in options:
        coordinator.set_min_soc(float(options["min_soc"]))
    if "max_soc" in options:
        coordinator.set_max_soc(float(options["max_soc"]))
    if "max_battery_power" in options:
        coordinator.set_max_battery_power(float(options["max_battery_power"]))
    if "send_interval" in options:
        coordinator.set_send_interval(float(options["send_interval"]))
    if "grid_smoothing" in options:
        coordinator.set_grid_smoothing_window(float(options["grid_smoothing"]))
    if "battery_smoothing" in options:
        coordinator.set_battery_smoothing_window(float(options["battery_smoothing"]))
    if "battery_capacity" in options:
        coordinator.set_battery_capacity_wh(float(options["battery_capacity"]))
    if "evening_min_soc" in options:
        coordinator.set_evening_min_soc(float(options["evening_min_soc"]))
    if "evening_max_charge_power" in options:
        coordinator.set_evening_max_charge_power(float(options["evening_max_charge_power"]))
    if "capacity_tariff_enabled" in options:
        coordinator.set_capacity_tariff_enabled(bool(options["capacity_tariff_enabled"]))
    if "max_desired_peak" in options:
        coordinator.set_max_desired_peak(float(options["max_desired_peak"]))
    if "manual_target_soc" in options:
        coordinator.set_manual_target_soc(float(options["manual_target_soc"]))
    if "manual_power" in options:
        coordinator.set_manual_power(float(options["manual_power"]))
    if "evening_peak_time" in options:
        raw = options["evening_peak_time"]
        if isinstance(raw, str) and ":" in raw:
            try:
                h, m = raw.split(":", 1)
                coordinator.set_evening_peak_time(time(int(h), int(m)))
            except (TypeError, ValueError):
                _LOGGER.warning("Invalid evening_peak_time in options: %s", raw)
    if "passive_floor_time" in options:
        raw = options["passive_floor_time"]
        if isinstance(raw, str) and ":" in raw:
            try:
                h, m = raw.split(":", 1)
                coordinator.set_passive_floor_time(time(int(h), int(m)))
            except (TypeError, ValueError):
                _LOGGER.warning("Invalid passive_floor_time in options: %s", raw)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Marstek Battery Controller from a config entry."""
    from homeassistant.exceptions import ConfigEntryNotReady
    from homeassistant.helpers import issue_registry as ir

    from .coordinator import CoordinatorRuntimeConfig, MarstekBatteryCoordinator
    from .discovery import resolve_roles_for_device, resolved_from_manual
    from .storage import MarstekStorage

    if entry.data.get(const.CONF_USE_DISCOVERY, False):
        device_id = entry.data.get(const.CONF_MARSTEK_DEVICE_ID)
        if not device_id:
            raise ConfigEntryNotReady("Missing Marstek device id")
        resolved = resolve_roles_for_device(hass, device_id)
        if resolved is None:
            ir.async_create_issue(
                hass,
                const.DOMAIN,
                const.ISSUE_MARSTEK_MISSING,
                breaks_invisibly=True,
                is_fixable=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key="marstek_unavailable",
            )
            raise ConfigEntryNotReady("marstek_modbus roles not resolved")
    else:
        manual = entry.data.get(const.CONF_MANUAL_ENTITIES)
        if not manual:
            raise ConfigEntryNotReady("Missing manual entity map")
        resolved = resolved_from_manual(manual)

    ir.async_delete_issue(hass, const.DOMAIN, const.ISSUE_MARSTEK_MISSING)

    storage = MarstekStorage(hass)
    store_data = await storage.async_load()
    prev_mode: str | None = store_data.get("previous_mode")

    runtime = CoordinatorRuntimeConfig(
        resolved=resolved,
        grid_entity_id=entry.data[const.CONF_GRID_POWER],
        cap_now_entity_id=entry.data.get(const.CONF_CAP_NOW_SENSOR),
        monthly_peak_entity_id=entry.data.get(const.CONF_MONTHLY_PEAK_SENSOR),
        use_internal_cap_now=not bool(entry.data.get(const.CONF_CAP_NOW_SENSOR)),
    )

    coordinator = MarstekBatteryCoordinator(
        hass,
        entry_id=entry.entry_id,
        runtime=runtime,
        storage=storage,
        initial_previous_mode=prev_mode,
    )

    _apply_options(coordinator, entry.options)

    hass.data.setdefault(const.DOMAIN, {})
    hass.data[const.DOMAIN][entry.entry_id] = coordinator

    await coordinator.async_config_entry_first_refresh()

    coordinator.async_setup_listeners()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload coordinator parameters when options change."""
    from .coordinator import MarstekBatteryCoordinator

    coordinator: MarstekBatteryCoordinator = hass.data[const.DOMAIN][entry.entry_id]
    _apply_options(coordinator, entry.options)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload config entry."""
    coordinator = hass.data[const.DOMAIN].pop(entry.entry_id)
    await coordinator.async_shutdown()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
