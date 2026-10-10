"""Delivery feedback for an already configured battery command.

Measured power is not the setpoint-deduplication baseline. It only answers
whether the battery is following the command the device already reports.
A missing publication identity is never treated as a new measurement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Omnibattery logs weak delivery below max(25 W, 10% of the command).
# The 10% rule is the initial criterion for ordinary commands of at least 100 W.
DELIVERY_FRACTION: Final = 0.10
DELIVERY_COMMAND_FLOOR_W: Final = 100.0
# Measured power at or above this, while a zero command is reported, has left idle.
IDLE_MEASURED_W: Final = 25.0


@dataclass
class NonDeliveryState:
    """Consecutive fresh shortfalls after the actuator has had time to move."""

    shortfall_count: int = 0
    first_shortfall_mono: float | None = None
    last_checked_command_w: int | None = None
    last_counted_token: str | None = None


@dataclass(frozen=True)
class NonDeliveryResult:
    """One evaluation. ``measured_w`` is a re-anchor suggestion, not an accept."""

    reanchor: bool
    measured_w: int | None
    shortfall_w: float


def observation_token(
    publication_id: str | None,
    source_mono: float | None,
) -> str | None:
    """Identity of one battery publication. None means "do not count"."""
    if publication_id:
        return f"id:{publication_id}"
    if source_mono is not None:
        return f"t:{source_mono:.9f}"
    return None


def delivery_threshold_w(command_w: int) -> float:
    """Omnibattery's initial low-power check: max(25 W, 10% of the command)."""
    return max(IDLE_MEASURED_W, DELIVERY_FRACTION * abs(float(command_w)))


def snap_noise_floor_command(command_w: int) -> int:
    """Return zero for a nonzero command inside the 25 W delivery floor.

    Delivery of a nonzero command requires at least 25 W of measured power in
    the commanded direction. A request of 1–24 W, positive or negative, can
    never pass that check, so leaving it in force mode would count as
    non-delivery forever. Marstek force-charge and force-discharge accept any
    nonzero wattage; there is no protocol minimum to round up to. Commands
    below the floor are therefore idle.
    """
    command = int(command_w)
    if command != 0 and abs(command) < IDLE_MEASURED_W:
        return 0
    return command


def delivering_command(command_w: int, measured_power_w: float) -> bool:
    """Whether measured power is far enough in the commanded direction.

    For a nonzero command the threshold is ``max(25 W, 10% of |command|)``.
    At or above ``DELIVERY_COMMAND_FLOOR_W`` that is Omnibattery's initial
    delivery criterion (10% once the command exceeds 250 W). Zero is idle
    when the measurement stays inside the 25 W floor.
    """
    if command_w == 0:
        return abs(measured_power_w) < IDLE_MEASURED_W
    threshold = delivery_threshold_w(command_w)
    if command_w > 0:
        return measured_power_w >= threshold
    return measured_power_w <= -threshold


def _reset(state: NonDeliveryState) -> None:
    state.shortfall_count = 0
    state.first_shortfall_mono = None
    state.last_counted_token = None


def evaluate_non_delivery(
    state: NonDeliveryState,
    *,
    accepted_command_w: int,
    measured_power_w: float | None,
    measured_fresh: bool,
    now_mono: float,
    last_write_mono: float | None,
    actuator_latency_s: float,
    consecutive_required: int = 3,
    fallback_timeout_s: float = 15.0,
    publication_id: str | None = None,
    source_mono: float | None = None,
) -> NonDeliveryResult:
    """Count persistent under-delivery of one configured command.

    The same battery publication — by id or by monotonic source time — counts
    once. Calls with neither identity do not count. Settling time and stale
    measurements do not count and do not erase progress. A command of 1–24 W
    in either direction is inside the noise floor and is idle, not a shortfall.
    """
    if abs(int(accepted_command_w)) < IDLE_MEASURED_W:
        _reset(state)
        state.last_checked_command_w = 0
        return NonDeliveryResult(reanchor=False, measured_w=None, shortfall_w=0.0)

    if not measured_fresh or measured_power_w is None:
        return NonDeliveryResult(reanchor=False, measured_w=None, shortfall_w=0.0)

    if last_write_mono is None or (now_mono - last_write_mono) < actuator_latency_s:
        return NonDeliveryResult(reanchor=False, measured_w=None, shortfall_w=0.0)

    token = observation_token(publication_id, source_mono)
    if token is None or token == state.last_counted_token:
        return NonDeliveryResult(reanchor=False, measured_w=None, shortfall_w=0.0)

    measured = float(measured_power_w)
    if accepted_command_w > 0:
        shortfall = float(accepted_command_w) - measured
    else:
        shortfall = measured - float(accepted_command_w)
    sufficient = delivering_command(accepted_command_w, measured)

    if sufficient:
        state.shortfall_count = 0
        state.first_shortfall_mono = None
        state.last_checked_command_w = accepted_command_w
        state.last_counted_token = token
        return NonDeliveryResult(
            reanchor=False,
            measured_w=None,
            shortfall_w=max(0.0, shortfall),
        )

    if state.last_checked_command_w != accepted_command_w:
        state.shortfall_count = 0
        state.first_shortfall_mono = None
        state.last_checked_command_w = accepted_command_w

    state.shortfall_count += 1
    state.last_counted_token = token
    if state.first_shortfall_mono is None:
        state.first_shortfall_mono = now_mono

    elapsed = now_mono - state.first_shortfall_mono
    if state.shortfall_count >= consecutive_required or elapsed >= fallback_timeout_s:
        state.shortfall_count = 0
        state.first_shortfall_mono = None
        return NonDeliveryResult(
            reanchor=True,
            measured_w=int(round(measured)),
            shortfall_w=shortfall,
        )

    return NonDeliveryResult(reanchor=False, measured_w=None, shortfall_w=shortfall)
