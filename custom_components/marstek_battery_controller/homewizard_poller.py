"""Async in-process poller for HomeWizard P1 grid power."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import time as time_mod

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import const

_LOGGER = logging.getLogger(__name__)


class HomeWizardPoller:
    """Polls /api/v1/data on a HomeWizard P1 at fixed intervals.

    Stores the most recent active_power_w value and a monotonic timestamp.
    Optionally invokes on_sample(value_w, monotonic_ts) on each successful read.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        ip: str,
        *,
        interval_s: float = const.DEFAULT_HOMEWIZARD_POLL_INTERVAL_S,
        on_sample: Callable[[float, float], None] | None = None,
    ) -> None:
        self._hass = hass
        self._ip = ip
        self._interval_s = float(interval_s)
        self._on_sample = on_sample
        self._latest_w: float | None = None
        self._latest_mono: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._consecutive_failures = 0
        self._warned_60_failures = False

    @property
    def latest_w(self) -> float | None:
        return self._latest_w

    @property
    def latest_mono(self) -> float | None:
        return self._latest_mono

    def is_fresh(self, *, now_mono: float | None = None) -> bool:
        """True iff there is a sample younger than HOMEWIZARD_FRESHNESS_LIMIT_S."""
        if self._latest_mono is None:
            return False
        now_ts = now_mono if now_mono is not None else time_mod.monotonic()
        return (now_ts - self._latest_mono) <= const.HOMEWIZARD_FRESHNESS_LIMIT_S

    async def start(self) -> None:
        """Start the polling task."""
        if self._task is not None and not self._task.done():
            return
        self._task = self._hass.loop.create_task(self._loop())

    async def stop(self) -> None:
        """Cancel the polling task and wait for clean shutdown."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _loop(self) -> None:
        """Internal: poll, parse, store, callback, sleep. Tolerate transient errors."""
        session = async_get_clientsession(self._hass)
        url = f"http://{self._ip}/api/v1/data"
        while True:
            try:
                timeout = aiohttp.ClientTimeout(total=const.HOMEWIZARD_API_TIMEOUT_S)
                async with session.get(url, timeout=timeout) as resp:
                    payload = await resp.json()
                value = float(payload["active_power_w"])
                mono = time_mod.monotonic()
                self._latest_w = value
                self._latest_mono = mono
                self._consecutive_failures = 0
                self._warned_60_failures = False
                if self._on_sample is not None:
                    try:
                        self._on_sample(value, mono)
                    except Exception as err:  # pragma: no cover - defensive
                        _LOGGER.debug("HomeWizard on_sample callback failed: %s", err)
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as err:
                self._consecutive_failures += 1
                _LOGGER.debug("HomeWizard poll failed (%s): %s", self._consecutive_failures, err)
                if self._consecutive_failures >= 60 and not self._warned_60_failures:
                    _LOGGER.warning(
                        "HomeWizard poll failed 60+ times consecutively for ip=%s",
                        self._ip,
                    )
                    self._warned_60_failures = True
            await asyncio.sleep(self._interval_s)
