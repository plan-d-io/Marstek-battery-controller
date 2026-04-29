"""Tests for HomeWizard discovery helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

pytest.importorskip("homeassistant")

from custom_components.marstek_battery_controller.homewizard_discovery import (
    list_homewizard_p1_candidates,
    probe_homewizard,
)


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
    def __init__(self, by_ip: dict[str, dict[str, Any]]) -> None:
        self._by_ip = by_ip

    def get(self, url: str, timeout: Any = None) -> _FakeResponse:  # noqa: ARG002
        ip = url.split("//", 1)[1].split("/", 1)[0]
        payload = self._by_ip[ip]
        return _FakeResponse(payload)


@dataclass
class _FakeEntry:
    entry_id: str
    title: str
    data: dict[str, Any]


class _FakeEntries:
    def __init__(self, entries: list[_FakeEntry]) -> None:
        self._entries = entries

    def async_entries(self, domain: str) -> list[_FakeEntry]:
        assert domain == "homewizard"
        return self._entries


class _FakeHass:
    def __init__(self, entries: list[_FakeEntry]) -> None:
        self.config_entries = _FakeEntries(entries)


@pytest.mark.asyncio
async def test_discovery_filters_only_p1(monkeypatch: pytest.MonkeyPatch) -> None:
    hass = _FakeHass(
        [
            _FakeEntry("1", "P1 meter", {"host": "10.0.0.2"}),
            _FakeEntry("2", "Slimme stekker", {"host": "10.0.0.3"}),
        ]
    )
    monkeypatch.setattr(
        "custom_components.marstek_battery_controller.homewizard_discovery.async_get_clientsession",
        lambda _hass: _FakeSession(
            {
                "10.0.0.2": {"product_type": "HWE-P1", "product_name": "P1 Meter"},
                "10.0.0.3": {"product_type": "HWE-SKT", "product_name": "Smart Plug"},
            }
        ),
    )
    cands = await list_homewizard_p1_candidates(hass)
    assert len(cands) == 1
    assert cands[0].ip == "10.0.0.2"
    assert cands[0].title == "P1 meter"


@pytest.mark.asyncio
async def test_probe_homewizard_success(monkeypatch: pytest.MonkeyPatch) -> None:
    hass = _FakeHass([])
    monkeypatch.setattr(
        "custom_components.marstek_battery_controller.homewizard_discovery.async_get_clientsession",
        lambda _hass: _FakeSession(
            {"192.168.1.20": {"product_type": "HWE-P1", "product_name": "P1 Meter"}}
        ),
    )
    cand = await probe_homewizard(hass, "192.168.1.20")
    assert cand is not None
    assert cand.product_type == "HWE-P1"
    assert cand.ip == "192.168.1.20"
