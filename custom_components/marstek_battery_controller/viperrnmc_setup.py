"""Inspect and apply Marstek Modbus (ViperRNMC) setup: entity enable + polling interval."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from time import monotonic

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations

from . import const
from .discovery import ResolvedEntities, resolve_roles_for_device
from .marstek_setup_i18n import build_marstek_setup_description_placeholders
from .viperrnmc_options import find_marstek_modbus_entry_for_device, get_high_interval

_LOGGER = logging.getLogger(__name__)

_ROLE_FIELD_BY_SUFFIX: dict[str, str] = {
    const.ROLE_BATTERY_SOC: "battery_soc",
    const.ROLE_AC_POWER: "ac_power",
    const.ROLE_RS485_CONTROL: "rs485_control",
    const.ROLE_FORCE_MODE: "force_mode",
    const.ROLE_SET_CHARGE_POWER: "set_charge_power",
    const.ROLE_SET_DISCHARGE_POWER: "set_discharge_power",
}


@dataclass(frozen=True)
class RoleStatus:
    """One required role and its entity registry state."""

    role: str
    entity_id: str
    disabled_by: er.RegistryEntryDisabler | None


@dataclass(frozen=True)
class MarstekSetupPlan:
    """What marstek_modbus changes the config flow may offer."""

    marstek_entry: ConfigEntry
    device_id: str
    resolved: ResolvedEntities
    roles: tuple[RoleStatus, ...]
    current_high_interval: int
    needs_enable: bool
    needs_interval: bool
    integration_disabled_roles: tuple[str, ...]
    user_disabled_roles: tuple[str, ...]

    @property
    def required_entity_ids(self) -> tuple[str, ...]:
        """All six role entity_ids in stable order."""
        return tuple(status.entity_id for status in self.roles)


def build_marstek_setup_plan(
    hass: HomeAssistant,
    device_id: str,
    marstek_entry: ConfigEntry,
) -> MarstekSetupPlan | None:
    """Build setup plan when all six roles exist in the registry (enabled or not)."""
    resolved = resolve_roles_for_device(hass, device_id)
    if resolved is None:
        return None

    ent_reg = er.async_get(hass)
    statuses: list[RoleStatus] = []
    integration_disabled: list[str] = []
    user_disabled: list[str] = []

    for suffix in sorted(const.ROLE_SUFFIXES):
        field = _ROLE_FIELD_BY_SUFFIX[suffix]
        entity_id = getattr(resolved, field)
        entry = ent_reg.async_get(entity_id)
        disabled_by = entry.disabled_by if entry is not None else None
        statuses.append(RoleStatus(role=suffix, entity_id=entity_id, disabled_by=disabled_by))
        if disabled_by is er.RegistryEntryDisabler.INTEGRATION:
            integration_disabled.append(suffix)
        elif disabled_by is er.RegistryEntryDisabler.USER:
            user_disabled.append(suffix)

    current = get_high_interval(marstek_entry)
    needs_interval = current > const.VIPER_HIGH_INTERVAL_RECOMMENDED
    needs_enable = bool(integration_disabled)

    return MarstekSetupPlan(
        marstek_entry=marstek_entry,
        device_id=device_id,
        resolved=resolved,
        roles=tuple(statuses),
        current_high_interval=current,
        needs_enable=needs_enable,
        needs_interval=needs_interval,
        integration_disabled_roles=tuple(integration_disabled),
        user_disabled_roles=tuple(user_disabled),
    )


def _entity_is_live(hass: HomeAssistant, entity_id: str) -> bool:
    state = hass.states.get(entity_id)
    return state is not None and state.state not in ("unknown", "unavailable", "")


async def wait_for_entities_live(
    hass: HomeAssistant,
    entity_ids: list[str] | tuple[str, ...],
    *,
    timeout_s: float = 90.0,
    poll_interval_s: float = 2.0,
) -> bool:
    """Return True when every entity_id has a non-unavailable state."""
    deadline = monotonic() + timeout_s
    pending = list(entity_ids)
    while monotonic() < deadline:
        pending = [eid for eid in pending if not _entity_is_live(hass, eid)]
        if not pending:
            return True
        await asyncio.sleep(poll_interval_s)
    _LOGGER.warning(
        "Timed out waiting for Marstek entities: %s",
        ", ".join(pending),
    )
    return False


async def apply_marstek_setup_plan(
    hass: HomeAssistant,
    plan: MarstekSetupPlan,
    *,
    enable_entities: bool,
    apply_interval: bool,
) -> bool:
    """Apply registry/options changes and reload marstek_modbus at most once."""
    ent_reg = er.async_get(hass)
    will_reload = False

    if enable_entities and plan.needs_enable:
        for status in plan.roles:
            if status.disabled_by is not er.RegistryEntryDisabler.INTEGRATION:
                continue
            ent_reg.async_update_entity(status.entity_id, disabled_by=None)
            _LOGGER.info("Enabled Marstek entity %s (%s)", status.entity_id, status.role)
        will_reload = True

    if apply_interval and plan.needs_interval:
        hass.config_entries.async_update_entry(
            plan.marstek_entry,
            options={
                **plan.marstek_entry.options,
                const.VIPER_HIGH_INTERVAL_KEY: const.VIPER_HIGH_INTERVAL_RECOMMENDED,
            },
        )
        will_reload = True

    if will_reload:
        await hass.config_entries.async_reload(plan.marstek_entry.entry_id)

    if enable_entities and plan.needs_enable:
        return await wait_for_entities_live(hass, plan.required_entity_ids)

    return True


async def async_build_marstek_setup_description_placeholders(
    hass: HomeAssistant,
    plan: MarstekSetupPlan,
) -> dict[str, str]:
    """Load config-flow translations and build description placeholders."""
    translations = await async_get_translations(
        hass,
        hass.config.language,
        "config",
        integrations=[const.DOMAIN],
    )
    return build_marstek_setup_description_placeholders(plan, translations)
