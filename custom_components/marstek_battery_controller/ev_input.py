"""EV sensor selection and this-boot publication parsing.

Home Assistant state objects stay outside this module. The coordinator passes
the fields it has already read.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any, Mapping

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import const
from .ev_feedback import normalize_ev_power
from .pd_session import ChannelSample

_LOGGER = logging.getLogger(__name__)

# psutil's process creation time and Home Assistant's last_reported clock can
# disagree by a small amount. A report this close to process start is still
# from the current process.
HA_PROCESS_START_TOLERANCE = timedelta(seconds=2)

_UNAVAILABLE = ChannelSample(value_w=None, available=False)
_MISSING_STATES = frozenset({None, "", "unknown", "unavailable"})


def clean_entity_id(raw: Any) -> str | None:
    """Return a stripped entity id, or None when the selector was cleared."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def resolve_ev_power_entity(
    options: Mapping[str, Any],
    data: Mapping[str, Any],
) -> str | None:
    """Resolve the configured EV sensor.

    A key present in options wins, including an explicit empty value that
    removes a sensor originally stored in the config entry data.
    """
    if const.CONF_EV_POWER in options:
        return clean_entity_id(options.get(const.CONF_EV_POWER))
    return clean_entity_id(data.get(const.CONF_EV_POWER))


def requested_exclude_ev(options: Mapping[str, Any], ev_entity_id: str | None) -> bool:
    """Stored choice, or true the first time a sensor is configured."""
    if const.CONF_EXCLUDE_EV in options:
        return bool(options[const.CONF_EXCLUDE_EV])
    return ev_entity_id is not None


def effective_exclude_ev(requested: bool, ev_entity_id: str | None) -> bool:
    """Exclusion follows the request only while a sensor is configured."""
    return bool(requested) and ev_entity_id is not None


def store_ev_sensor_choice(
    options: dict[str, Any],
    user_input: Mapping[str, Any],
    previous_entity: str | None,
) -> str | None:
    """Record the options-flow sensor choice, including an explicit clear.

    An omitted or blank selector clears the sensor. It is not filled back in
    from the previous value. Removing the sensor keeps an explicit request and,
    when the request was only the first-sensor default, stores that default.
    """
    if const.CONF_EV_POWER in user_input:
        entity = clean_entity_id(user_input.get(const.CONF_EV_POWER))
    else:
        entity = None
    options[const.CONF_EV_POWER] = entity
    if const.CONF_EXCLUDE_EV not in options and previous_entity and entity is None:
        options[const.CONF_EXCLUDE_EV] = True
    return entity


def read_ha_process_started_utc() -> datetime:
    """Return this Python process's creation time as a timezone-aware UTC datetime.

    The psutil lookup can block. Call it through ``async_add_executor_job``.
    """
    import psutil_home_assistant as ha_psutil

    created = ha_psutil.PsutilWrapper().psutil.Process().create_time()
    started = dt_util.utc_from_timestamp(float(created))
    if started.tzinfo is None:
        raise ValueError("Home Assistant process start time has no timezone")
    return started


async def async_resolve_ha_process_started_utc(hass: HomeAssistant) -> datetime:
    """Process start time, or the setup time when that lookup fails.

    Failure is conservative: EV reports from before this setup stay unused
    until the sensor reports again. Setup itself continues.
    """
    try:
        return await hass.async_add_executor_job(read_ha_process_started_utc)
    except Exception:
        _LOGGER.warning(
            "Could not read the Home Assistant process start time; "
            "EV reports from before this setup are ignored until the sensor reports again",
            exc_info=True,
        )
        return dt_util.utcnow()


def ev_channel_sample(
    *,
    entity_id: str,
    raw_state: str | None,
    unit: str | None,
    restored: bool,
    last_reported: datetime | None,
    ha_process_started_utc: datetime | None,
    now_mono: float,
    now_wall: datetime,
) -> ChannelSample:
    """Build one EV sample, or an unavailable sample when it cannot be used.

    ``last_updated`` is intentionally not an argument. A missing
    ``last_reported``, a restored state, or a report from before this Home
    Assistant process is unavailable. Reports up to
    ``HA_PROCESS_START_TOLERANCE`` before the process start still count, because
    the two clocks are not identical. The same accepted report keeps one
    publication id so later calculator ticks do not look like new observations.
    """
    if (
        restored
        or last_reported is None
        or ha_process_started_utc is None
        or raw_state in _MISSING_STATES
        or last_reported.tzinfo is None
        or ha_process_started_utc.tzinfo is None
        or last_reported < ha_process_started_utc - HA_PROCESS_START_TOLERANCE
    ):
        return _UNAVAILABLE
    normalized = normalize_ev_power(raw_state, unit)
    if not normalized.usable or normalized.watts is None:
        return _UNAVAILABLE
    try:
        age = max(0.0, (now_wall - last_reported).total_seconds())
        publication_id = f"{entity_id}:{last_reported.timestamp()}"
    except (TypeError, AttributeError, OSError, ValueError):
        return _UNAVAILABLE
    return ChannelSample(
        value_w=float(normalized.watts),
        publication_id=publication_id,
        source_mono=now_mono,
        wall_age_s=age,
    )
