"""Resolve marstek_modbus entity roles via device and entity registries."""

# TODO: Confirm with a live marstek_modbus device whether unique_ids always use a single
# unambiguous suffix; longest-suffix-first matching is used when multiple roles could match.

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from . import const

_LOGGER = logging.getLogger(__name__)

MARSTEK_DOMAIN = "marstek_modbus"


@dataclass(frozen=True)
class ResolvedEntities:
    """entity_id strings for the six Marstek control/read roles."""

    battery_soc: str
    ac_power: str
    rs485_control: str
    force_mode: str
    set_charge_power: str
    set_discharge_power: str


def list_marstek_devices(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Return (device_id, display_name) for each device from marstek_modbus."""
    dev_reg = dr.async_get(hass)
    out: list[tuple[str, str]] = []
    for cfg in hass.config_entries.async_entries(MARSTEK_DOMAIN):
        for device_entry in dr.async_entries_for_config_entry(dev_reg, cfg.entry_id):
            device_id = device_entry.id
            name = device_entry.name_by_user or device_entry.name or device_id
            out.append((device_id, name))
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for did, name in out:
        if did not in seen:
            seen.add(did)
            unique.append((did, name))
    return unique


def resolve_roles_for_device(hass: HomeAssistant, device_id: str) -> ResolvedEntities | None:
    """Map unique_id suffixes to entity_ids for one device."""
    ent_reg = er.async_get(hass)
    roles: dict[str, str] = {}
    for entity_entry in er.async_entries_for_device(ent_reg, device_id):
        uid_raw = entity_entry.unique_id
        try:
            uid = "" if uid_raw is None else str(uid_raw)
        except Exception:
            _LOGGER.warning(
                "Skipping entity %s with non-stringifiable unique_id",
                entity_entry.entity_id,
            )
            continue
        if not uid:
            continue
        matched: str | None = None
        for suffix in sorted(const.ROLE_SUFFIXES, key=len, reverse=True):
            if uid.endswith(suffix):
                matched = suffix
                break
        if matched is None:
            continue
        if matched in roles:
            _LOGGER.warning(
                "Duplicate role %s on device %s (%s vs %s); keeping first",
                matched,
                device_id,
                roles[matched],
                entity_entry.entity_id,
            )
            continue
        roles[matched] = entity_entry.entity_id

    missing = const.ROLE_SUFFIXES - frozenset(roles.keys())
    if missing:
        _LOGGER.warning(
            "Marstek device %s missing roles: %s",
            device_id,
            ", ".join(sorted(missing)),
        )
        return None

    return ResolvedEntities(
        battery_soc=roles[const.ROLE_BATTERY_SOC],
        ac_power=roles[const.ROLE_AC_POWER],
        rs485_control=roles[const.ROLE_RS485_CONTROL],
        force_mode=roles[const.ROLE_FORCE_MODE],
        set_charge_power=roles[const.ROLE_SET_CHARGE_POWER],
        set_discharge_power=roles[const.ROLE_SET_DISCHARGE_POWER],
    )


def resolved_from_manual(m: dict[str, str]) -> ResolvedEntities:
    """Build ResolvedEntities from config flow manual map (all six required)."""
    return ResolvedEntities(
        battery_soc=m[const.CONF_ENTITY_BATTERY_SOC],
        ac_power=m[const.CONF_ENTITY_AC_POWER],
        rs485_control=m[const.CONF_ENTITY_RS485],
        force_mode=m[const.CONF_ENTITY_FORCE_MODE],
        set_charge_power=m[const.CONF_ENTITY_SET_CHARGE],
        set_discharge_power=m[const.CONF_ENTITY_SET_DISCHARGE],
    )
