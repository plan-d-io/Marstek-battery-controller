"""Tests for HomeWizard fast poller."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("homeassistant")

from custom_components.marstek_battery_controller import const
from custom_components.marstek_battery_controller.homewizard_poller import HomeWizardPoller


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def json(self) -> dict[str, Any]:
        return self._payload


class _FakeSession:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def get(self, url: str, timeout: Any = None) -> _FakeResponse:  # noqa: ARG002
        return _FakeResponse(self._payload)


@pytest.mark.asyncio
async def test_poller_stores_latest_sample_and_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poller should parse active_power_w and invoke callback."""
    seen: list[tuple[float, float]] = []

    def _on_sample(value_w: float, mono: float) -> None:
        seen.append((value_w, mono))

    hass = SimpleNamespace(loop=asyncio.get_running_loop())
    monkeypatch.setattr(
        "custom_components.marstek_battery_controller.homewizard_poller.async_get_clientsession",
        lambda _hass: _FakeSession({"active_power_w": 321.0}),
    )
    poller = HomeWizardPoller(hass, "1.2.3.4", interval_s=0.05, on_sample=_on_sample)
    await poller.start()
    await asyncio.sleep(0.12)
    await poller.stop()

    assert poller.latest_w == pytest.approx(321.0)
    assert poller.latest_mono is not None
    assert poller.is_fresh(now_mono=poller.latest_mono + const.HOMEWIZARD_FRESHNESS_LIMIT_S - 0.1)
    assert seen
    assert seen[-1][0] == pytest.approx(321.0)
