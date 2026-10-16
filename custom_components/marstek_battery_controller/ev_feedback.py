"""Pure EV-exclusion feedback for the existing battery controller.

Home Assistant stays outside this module. Signs match the rest of the
controller: grid import and battery discharge are positive, battery charge
is negative, and EV power is consumption.

The site balance with the battery removed is ``N = G + B``. The battery
power the controller should approach is ``B* = max(0, N - E) + min(0, N)``.
Feedback is ``F = B* - B``. That is not ``G - E``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Final

from .non_delivery import IDLE_MEASURED_W

# Readings strictly inside this floor are idle, same as measured battery power.
EV_IDLE_FLOOR_W: Final = IDLE_MEASURED_W
_WATT = "w"
_KILOWATT = "kw"


class EvPowerVerdict(Enum):
    """Whether an EV power reading can enter the known-EV equation."""

    POSITIVE = "positive"
    ZERO = "zero"
    UNUSABLE = "unusable"


@dataclass(frozen=True)
class NormalizedEvPower:
    """One EV power reading after unit conversion and the idle floor.

    ``watts`` is ``None`` only for ``UNUSABLE``. Zero is a real usable reading.
    """

    verdict: EvPowerVerdict
    watts: float | None

    @property
    def usable(self) -> bool:
        return self.verdict is not EvPowerVerdict.UNUSABLE


@dataclass(frozen=True)
class EvExclusionFeedback:
    """Permission and feedback for one exclusion step.

    ``feedback_w`` is what a later layer may pass to the unchanged PD
    controller. ``None`` means there is no PD feedback: both directions are
    closed and the session must request zero through its existing permission
    path. This model never requests Released.
    """

    allow_discharge: bool
    allow_charge: bool
    feedback_w: float | None
    target_battery_w: float | None
    site_balance_w: float | None
    request_released: bool = False


def normalize_ev_power(value: object, unit: str | None) -> NormalizedEvPower:
    """Convert one EV power sample to watts, or mark it unusable.

    ``W`` and ``kW`` are accepted in any case. ``kW`` is multiplied by 1000.
    A missing unit, any other unit, a non-numeric value, NaN, or infinity is
    unusable. A value at or below ``-EV_IDLE_FLOOR_W`` is materially negative
    and stays unusable; its absolute value is not used. Every other reading
    strictly inside the floor, including a small negative, is usable zero.
    ``EV_IDLE_FLOOR_W`` itself remains a positive load.
    """
    watts = _reading_watts(value, unit)
    if watts is None or watts <= -EV_IDLE_FLOOR_W:
        return NormalizedEvPower(EvPowerVerdict.UNUSABLE, None)
    if watts < EV_IDLE_FLOOR_W:
        return NormalizedEvPower(EvPowerVerdict.ZERO, 0.0)
    return NormalizedEvPower(EvPowerVerdict.POSITIVE, watts)


def known_ev_feedback(grid_w: float, battery_w: float, ev_w: float) -> EvExclusionFeedback:
    """Feedback for a usable EV power, including usable zero.

    ``ev_w`` must already be normalized and therefore cannot be negative.
    When ``N >= 0``, ``B*`` is not negative and charge is not permitted.
    When ``N < 0``, ``B*`` is that export and charging it remains permitted.
    """
    ev = float(ev_w)
    if ev < 0.0 or not math.isfinite(ev):
        raise ValueError("EV power must be a finite normalized reading")
    grid = _finite(grid_w, "grid")
    battery = _finite(battery_w, "battery")
    site = grid + battery
    non_ev = site - ev
    target = max(0.0, non_ev) + min(0.0, site)
    return EvExclusionFeedback(
        allow_discharge=target > 0.0,
        allow_charge=target < 0.0,
        feedback_w=target - battery,
        target_battery_w=target,
        site_balance_w=site,
    )


def unusable_ev_feedback(grid_w: float, *, grid_fresh: bool) -> EvExclusionFeedback:
    """Safe policy when EV power cannot be used.

    Fresh physical export (``grid_fresh`` and ``G < 0``) may still charge the
    battery. Feedback is that raw grid power, not an adjusted target.
    Every other grid state closes charge and discharge and supplies no PD
    feedback, so the session can zero the request without entering Released.
    """
    grid = _finite(grid_w, "grid")
    if grid_fresh and grid < 0.0:
        return EvExclusionFeedback(
            allow_discharge=False,
            allow_charge=True,
            feedback_w=grid,
            target_battery_w=None,
            site_balance_w=None,
        )
    return EvExclusionFeedback(
        allow_discharge=False,
        allow_charge=False,
        feedback_w=None,
        target_battery_w=None,
        site_balance_w=None,
    )


def ev_exclusion_feedback(
    grid_w: float,
    battery_w: float,
    ev_power: NormalizedEvPower,
    *,
    grid_fresh: bool,
) -> EvExclusionFeedback:
    """Choose known-EV feedback or the unusable-EV policy."""
    if ev_power.verdict is EvPowerVerdict.UNUSABLE or ev_power.watts is None:
        return unusable_ev_feedback(grid_w, grid_fresh=grid_fresh)
    return known_ev_feedback(grid_w, battery_w, ev_power.watts)


def _reading_watts(value: object, unit: str | None) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        watts = float(value)
    elif isinstance(value, str):
        try:
            watts = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(watts):
        return None
    token = unit.strip().casefold() if isinstance(unit, str) else ""
    if token == _WATT:
        return watts
    if token == _KILOWATT:
        return watts * 1000.0
    return None


def _finite(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} power must be finite")
    return number
