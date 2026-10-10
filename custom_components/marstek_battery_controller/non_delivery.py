"""Detect sustained battery non-delivery without treating it as a new command.

The helper only reports that the PD baseline should move to the measured
power. It never writes accepted-command state. Stale samples and repeated
publication ids do not change the shortfall count.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class NonDeliveryState:
    """Consecutive fresh shortfalls after the actuator has had time to move."""

    shortfall_count: int = 0
    first_shortfall_mono: float | None = None
    last_checked_command_w: int | None = None
    last_counted_publication_id: str | None = None


@dataclass(frozen=True)
class NonDeliveryResult:
    """One evaluation. ``measured_w`` is a re-anchor suggestion, not an accept."""

    reanchor: bool
    measured_w: int | None
    shortfall_w: float


def _reset(state: NonDeliveryState) -> None:
    state.shortfall_count = 0
    state.first_shortfall_mono = None
    state.last_counted_publication_id = None


def evaluate_non_delivery(
    state: NonDeliveryState,
    *,
    accepted_command_w: int,
    measured_power_w: float | None,
    measured_fresh: bool,
    now_mono: float,
    last_write_mono: float | None,
    actuator_latency_s: float,
    shortfall_threshold_w: float = 150.0,
    consecutive_required: int = 3,
    fallback_timeout_s: float = 15.0,
    publication_id: str | None = None,
) -> NonDeliveryResult:
    """Count persistent divergence between an accepted command and fresh power.

    Compare against the accepted command, not the PD reference. A shortfall
    during actuator settle is ignored. The same publication cannot increment
    the count again. A stale measurement is ignored and does not erase progress.
    """
    if accepted_command_w == 0:
        _reset(state)
        state.last_checked_command_w = 0
        return NonDeliveryResult(reanchor=False, measured_w=None, shortfall_w=0.0)

    if not measured_fresh or measured_power_w is None:
        return NonDeliveryResult(reanchor=False, measured_w=None, shortfall_w=0.0)

    if last_write_mono is None or (now_mono - last_write_mono) < actuator_latency_s:
        return NonDeliveryResult(reanchor=False, measured_w=None, shortfall_w=0.0)

    if publication_id is not None and publication_id == state.last_counted_publication_id:
        return NonDeliveryResult(reanchor=False, measured_w=None, shortfall_w=0.0)

    measured = float(measured_power_w)
    if accepted_command_w > 0:
        shortfall = float(accepted_command_w) - measured
    else:
        shortfall = measured - float(accepted_command_w)

    if shortfall < shortfall_threshold_w:
        state.shortfall_count = 0
        state.first_shortfall_mono = None
        state.last_checked_command_w = accepted_command_w
        state.last_counted_publication_id = publication_id
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
    state.last_counted_publication_id = publication_id
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
