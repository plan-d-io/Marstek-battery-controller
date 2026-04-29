"""Discovery/probing helpers for HomeWizard P1 devices."""

from __future__ import annotations

from dataclasses import dataclass
import asyncio

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import const


@dataclass(frozen=True)
class HomeWizardCandidate:
    """One discovered HomeWizard device."""

    entry_id: str | None
    ip: str
    product_type: str
    product_name: str
    title: str


async def probe_homewizard(hass: HomeAssistant, ip: str) -> HomeWizardCandidate | None:
    """GET /api on the given IP. Return a candidate, or None on any failure."""
    session = async_get_clientsession(hass)
    url = f"http://{ip}/api"
    try:
        timeout = aiohttp.ClientTimeout(total=const.HOMEWIZARD_API_TIMEOUT_S)
        async with session.get(url, timeout=timeout) as resp:
            payload = await resp.json()
        ptype = str(payload["product_type"])
        pname = str(payload["product_name"])
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError):
        return None
    return HomeWizardCandidate(
        entry_id=None,
        ip=ip,
        product_type=ptype,
        product_name=pname,
        title="Manual entry",
    )


async def list_homewizard_p1_candidates(hass: HomeAssistant) -> list[HomeWizardCandidate]:
    """Return all HomeWizard config entries whose /api reports product_type == HWE-P1."""
    out: list[HomeWizardCandidate] = []
    entries: list[ConfigEntry] = list(hass.config_entries.async_entries("homewizard"))
    for entry in entries:
        ip = entry.data.get("host") or entry.data.get("ip_address")
        if not ip:
            continue
        cand = await probe_homewizard(hass, str(ip))
        if cand is None or cand.product_type != const.HOMEWIZARD_PRODUCT_P1:
            continue
        out.append(
            HomeWizardCandidate(
                entry_id=entry.entry_id,
                ip=cand.ip,
                product_type=cand.product_type,
                product_name=cand.product_name,
                title=entry.title or cand.product_name,
            )
        )
    return out
