"""Persistent storage for manual-mode previous_mode (§17)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from . import const


class MarstekStorage:
    """Thin wrapper around Store for integration JSON."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind to Home Assistant Store."""
        self._store = Store(hass, const.STORAGE_VERSION, const.STORAGE_KEY)

    async def async_load(self) -> dict[str, Any]:
        """Load persisted data or return defaults."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return {"previous_mode": None, "version": const.STORAGE_VERSION}
        out = dict(data)
        out.setdefault("previous_mode", None)
        out.setdefault("version", const.STORAGE_VERSION)
        return out

    async def async_save_previous_mode(self, previous_mode: str | None) -> None:
        """Persist previous_mode for restart resume."""
        current = await self.async_load()
        current["previous_mode"] = previous_mode
        current["version"] = const.STORAGE_VERSION
        await self._store.async_save(current)
