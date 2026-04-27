"""Modbus-related writes via marstek_modbus entities (service calls)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from . import const

if TYPE_CHECKING:
    from .discovery import ResolvedEntities

_LOGGER = logging.getLogger(__name__)


class MarstekModbusWriter:
    """Encapsulates RS485 / force mode / charge / discharge writes with spacing."""

    def __init__(
        self,
        hass: HomeAssistant,
        entities: "ResolvedEntities",
    ) -> None:
        """Initialize with resolved marstek_modbus entity ids."""
        self._hass = hass
        self._e = entities
        self._rs485_on: bool = False

    @property
    def rs485_on(self) -> bool:
        """Whether the writer last turned RS485 control on."""
        return self._rs485_on

    def set_rs485_state(self, on: bool) -> None:
        """Synchronize assumed RS485 state after external changes (optional)."""
        self._rs485_on = on

    async def _sleep_write_delay(self) -> None:
        await asyncio.sleep(const.MODBUS_WRITE_DELAY_S)

    async def async_call_switch(self, entity_id: str, service: str) -> None:
        """switch.turn_on / turn_off."""
        domain = entity_id.split(".", 1)[0]
        await self._hass.services.async_call(
            domain,
            service,
            {"entity_id": entity_id},
            blocking=True,
        )

    async def async_select_option(self, entity_id: str, option: str) -> None:
        """select.select_option."""
        await self._hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id, "option": option},
            blocking=True,
        )

    async def async_number_set_value(self, entity_id: str, value: float) -> None:
        """number.set_value."""
        await self._hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id, "value": value},
            blocking=True,
        )

    async def async_send_setpoint(self, setpoint_w: int) -> None:
        """§12.2 — translate signed setpoint to four underlying writes."""
        if setpoint_w == 0:
            force_mode = const.FORCE_STANDBY
            charge_w = 0.0
            discharge_w = 0.0
        elif setpoint_w > 0:
            force_mode = const.FORCE_DISCHARGE
            charge_w = 0.0
            discharge_w = float(abs(setpoint_w))
        else:
            force_mode = const.FORCE_CHARGE
            charge_w = float(abs(setpoint_w))
            discharge_w = 0.0

        if not self._rs485_on:
            await self.async_call_switch(self._e.rs485_control, "turn_on")
            await self._sleep_write_delay()
            self._rs485_on = True

        await self.async_select_option(self._e.force_mode, force_mode)
        await self._sleep_write_delay()

        if force_mode == const.FORCE_DISCHARGE:
            await self.async_number_set_value(self._e.set_discharge_power, discharge_w)
            await self._sleep_write_delay()
            await self.async_number_set_value(self._e.set_charge_power, 0.0)
        elif force_mode == const.FORCE_CHARGE:
            await self.async_number_set_value(self._e.set_charge_power, charge_w)
            await self._sleep_write_delay()
            await self.async_number_set_value(self._e.set_discharge_power, 0.0)
        else:
            await self.async_number_set_value(self._e.set_discharge_power, 0.0)
            await self._sleep_write_delay()
            await self.async_number_set_value(self._e.set_charge_power, 0.0)

    async def async_cleanup_released_sequence(self) -> None:
        """§6.1 — one-shot sequence when entering Released."""
        await self.async_call_switch(self._e.rs485_control, "turn_on")
        await self._sleep_write_delay()
        self._rs485_on = True

        await self.async_select_option(self._e.force_mode, const.FORCE_STANDBY)
        await self._sleep_write_delay()

        await self.async_number_set_value(self._e.set_discharge_power, 0.0)
        await self._sleep_write_delay()
        await self.async_number_set_value(self._e.set_charge_power, 0.0)
        await self._sleep_write_delay()

        await self.async_call_switch(self._e.rs485_control, "turn_off")
        await self._sleep_write_delay()
        self._rs485_on = False
