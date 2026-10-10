"""Pure time-aware PD battery controller.

No Home Assistant imports. Callers inject monotonic timestamps.

Sign convention (production Marstek, not a guess):

* Positive watts are discharge / grid import.
* Negative watts are charge / grid export.
* Zero is idle / no exchange.

``modbus_writer.async_send_setpoint`` maps ``setpoint_w > 0`` to force discharge
and ``setpoint_w < 0`` to force charge. ``calculator`` agrees: a positive grid
reading produces a positive (discharge) base, boost and manual charge commands
are negative, and the SoC floor blocks a positive setpoint.

Feedback law, evaluated at most once per fresh grid publication::

    error = filtered_grid - target_grid
    command = reference + kp * error * p_scale + kd * filtered_derivative

``reference`` is the accepted command, or a measurement re-anchor that is not
itself an accepted command. Positive error (import) increases discharge.
Omnibattery uses the opposite actuator sign and subtracts the same correction.

Watchdog and stale calls repeat the last proposal. They do not update the
filter, the derivative, the publication identity, or the zero-cross timer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .const import DEFAULT_MAX_BATTERY_POWER
from .publications import PublicationTracker

# Nominal period the proportional gain and ramp were tuned against.
NOMINAL_CYCLE_S: Final = 2.0
# Sub-second samples stay accurate; one delayed sample cannot integrate for minutes.
P_DT_MIN_S: Final = 0.1
P_DT_MAX_S: Final = 10.0
# Derivative divisor floor. Dividing by a short dt turns meter noise into a kick.
DERIVATIVE_DT_FLOOR_S: Final = 1.0
# Grid-step snap threshold uses max(3 * deadband, this floor).
STEP_FLOOR_W: Final = 200.0
# Zero-cross dwell floor. The effective hold is max(this, 2 * actuator latency).
DEFAULT_ZERO_CROSS_HOLD_S: Final = 5.0

# Internal defaults follow Omnibattery's balanced PD profile. They are not
# Home Assistant controls; Milestone 1 does not expose them.
DEFAULT_KP: Final = 0.35
DEFAULT_KD: Final = 0.30
DEFAULT_DEADBAND_W: Final = 40.0
DEFAULT_MAX_POWER_CHANGE_W: Final = 800.0
DEFAULT_DIRECTION_HYSTERESIS_W: Final = 60.0
DEFAULT_GRID_EMA_TAU_S: Final = 2.0
DEFAULT_DERIVATIVE_EMA_TAU_S: Final = 3.0
# Longer than Omnibattery's direct Marstek latency (0.8 s) because this
# integration reaches the battery through several Home Assistant service calls.
DEFAULT_ACTUATOR_LATENCY_S: Final = 3.0


@dataclass
class PDParams:
    """Controller parameters. Defaults are conservative and internal."""

    kp: float = DEFAULT_KP
    kd: float = DEFAULT_KD
    max_power_change_w: float = DEFAULT_MAX_POWER_CHANGE_W
    deadband_w: float = DEFAULT_DEADBAND_W
    direction_hysteresis_w: float = DEFAULT_DIRECTION_HYSTERESIS_W
    grid_ema_tau_s: float = DEFAULT_GRID_EMA_TAU_S
    derivative_ema_tau_s: float = DEFAULT_DERIVATIVE_EMA_TAU_S
    battery_actuator_latency_s: float = DEFAULT_ACTUATOR_LATENCY_S
    max_battery_power_w: float = DEFAULT_MAX_BATTERY_POWER


@dataclass
class PDState:
    """Mutable filter, publication, and zero-cross memory."""

    grid_filtered: float | None = None
    previous_error: float = 0.0
    derivative_filtered: float = 0.0
    derivative_ready: bool = False
    suppress_derivative_once: bool = False
    last_integrate_mono: float | None = None
    last_publication_id: str | None = None
    last_source_mono: float | None = None
    # Last non-zero direction: +1 discharge, -1 charge, 0 idle.
    last_direction: int = 0
    opposite_since_mono: float | None = None
    zero_cross_holding: bool = False
    last_error: float = 0.0
    last_p_term: float = 0.0
    last_d_term: float = 0.0
    last_slew_limited: bool = False
    last_in_deadband: bool = False
    last_proposed_w: int | None = None


@dataclass(frozen=True)
class PDStepResult:
    """One proposal. This is not an accepted command until a write succeeds."""

    proposed_command_w: int
    filtered_grid_w: float
    error_w: float
    p_term: float
    d_term: float
    in_deadband: bool
    slew_limited: bool
    clamped: bool
    zero_cross_hold: bool
    integrate_applied: bool
    reason_code: str | None = None


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _ema(previous: float, raw: float, dt: float, tau: float) -> float:
    alpha = dt / (tau + dt) if tau > 0 else 1.0
    return previous + alpha * (raw - previous)


def zero_cross_hold_s(params: PDParams) -> float:
    """How long an opposite-direction demand must persist before a reversal."""
    return max(DEFAULT_ZERO_CROSS_HOLD_S, 2.0 * params.battery_actuator_latency_s)


def reset_pd_state(state: PDState) -> None:
    """Clear filter, derivative, publication, and zero-cross memory."""
    state.grid_filtered = None
    state.previous_error = 0.0
    state.derivative_filtered = 0.0
    state.derivative_ready = False
    state.suppress_derivative_once = False
    state.last_integrate_mono = None
    state.last_publication_id = None
    state.last_source_mono = None
    state.last_direction = 0
    state.opposite_since_mono = None
    state.zero_cross_holding = False
    state.last_error = 0.0
    state.last_p_term = 0.0
    state.last_d_term = 0.0
    state.last_slew_limited = False
    state.last_in_deadband = False
    state.last_proposed_w = None


def reanchor_pd_state(state: PDState, measured_power_w: float) -> None:
    """Drop windup after sustained non-delivery.

    The measured wattage becomes the derivative/direction baseline only. It is
    not an accepted command; the caller keeps that on the command ledger.
    """
    state.previous_error = 0.0
    state.derivative_filtered = 0.0
    state.suppress_derivative_once = True
    state.opposite_since_mono = None
    state.zero_cross_holding = False
    state.last_direction = _sign(measured_power_w)
    state.last_slew_limited = False
    # Next watchdog repeats this baseline. It is a proposal, not an accept.
    state.last_proposed_w = int(round(measured_power_w))


def _can_integrate(
    state: PDState,
    *,
    integrate: bool,
    fresh: bool,
    publication_id: str | None,
    source_mono: float | None,
) -> bool:
    """A publication may advance the controller at most once, and only while fresh."""
    if not integrate or not fresh:
        return False
    if publication_id is not None and publication_id == state.last_publication_id:
        return False
    if (
        source_mono is not None
        and state.last_source_mono is not None
        and source_mono <= state.last_source_mono + 1e-9
    ):
        return False
    return True


def _apply_permissions(proposed: int, *, allow_charge: bool, allow_discharge: bool) -> int:
    if proposed > 0 and not allow_discharge:
        return 0
    if proposed < 0 and not allow_charge:
        return 0
    return proposed


def _clamp_command(proposed: float, params: PDParams) -> tuple[int, bool]:
    limited = _clamp(proposed, -params.max_battery_power_w, params.max_battery_power_w)
    clamped = abs(limited - proposed) > 0.5
    return int(round(limited)), clamped


def _frozen_result(
    state: PDState,
    params: PDParams,
    *,
    grid_raw_w: float,
    reference_command_w: int,
    allow_charge: bool,
    allow_discharge: bool,
    reason_code: str,
) -> PDStepResult:
    """Repeat the last proposal without integrating, deriving, or moving the hold."""
    holding = state.opposite_since_mono is not None
    if holding:
        proposed = 0
    elif state.last_proposed_w is not None:
        proposed = state.last_proposed_w
    else:
        proposed = int(reference_command_w)
    proposed = _apply_permissions(
        proposed, allow_charge=allow_charge, allow_discharge=allow_discharge
    )
    proposed, clamped = _clamp_command(float(proposed), params)
    filtered = state.grid_filtered if state.grid_filtered is not None else float(grid_raw_w)
    reason = reason_code
    if proposed == 0 and not allow_discharge and (state.last_proposed_w or 0) > 0:
        reason = "discharge_blocked"
    elif proposed == 0 and not allow_charge and (state.last_proposed_w or 0) < 0:
        reason = "charge_blocked"
    return PDStepResult(
        proposed_command_w=proposed,
        filtered_grid_w=filtered,
        error_w=state.last_error,
        p_term=0.0,
        d_term=0.0,
        in_deadband=state.last_in_deadband,
        slew_limited=False,
        clamped=clamped,
        zero_cross_hold=holding,
        integrate_applied=False,
        reason_code=reason,
    )


def _apply_direction_gate(
    state: PDState,
    params: PDParams,
    *,
    proposed_w: int,
    reference_w: int,
    error_w: float,
    now_mono: float,
) -> tuple[int, bool, str | None]:
    """Hold at zero until an opposite demand has persisted.

    Same-direction corrections pass. A demand that returns before the hold
    expires cancels the pending reversal, so a later sample cannot inherit it.
    """
    reference_dir = _sign(reference_w) or state.last_direction
    intent_dir = _sign(error_w) if abs(error_w) > params.deadband_w else 0
    hold_needed = zero_cross_hold_s(params)

    if (
        state.opposite_since_mono is not None
        and reference_dir != 0
        and intent_dir in (0, reference_dir)
    ):
        state.opposite_since_mono = None

    if reference_dir != 0 and intent_dir in (0, reference_dir):
        state.opposite_since_mono = None
        return proposed_w, False, None

    if reference_dir == 0:
        if (
            abs(proposed_w) < params.direction_hysteresis_w
            and abs(error_w) < params.direction_hysteresis_w
        ):
            return 0, False, None
        state.opposite_since_mono = None
        return proposed_w, False, None

    # Too small to justify a reversal. Keep the same-direction correction.
    if abs(error_w) < params.direction_hysteresis_w:
        if _sign(proposed_w) in (0, reference_dir):
            return proposed_w, False, None
        return reference_w, False, None

    if state.opposite_since_mono is None:
        state.opposite_since_mono = now_mono
        return 0, True, "pd_zero_cross_hold"

    if now_mono - state.opposite_since_mono < hold_needed:
        return 0, True, "pd_zero_cross_hold"

    state.opposite_since_mono = None
    # Still on the old side of zero: stop, and let the next sample leave from 0.
    if _sign(proposed_w) == reference_dir:
        return 0, False, None
    return proposed_w, False, None


def pd_step(
    state: PDState,
    params: PDParams,
    *,
    grid_raw_w: float,
    now_mono: float,
    reference_command_w: int,
    integrate: bool = True,
    fresh: bool = True,
    publication_id: str | None = None,
    source_mono: float | None = None,
    target_grid_w: float = 0.0,
    force_zero: bool = False,
    allow_charge: bool = True,
    allow_discharge: bool = True,
) -> PDStepResult:
    """Propose a signed battery command from one grid observation.

    ``reference_command_w`` is the incremental baseline (accepted command, or a
    measurement re-anchor). The returned proposal is not accepted state.
    """
    if force_zero:
        # Consume a fresh publication so the same sample cannot integrate after
        # the block lifts. Do not move the filter, derivative, or integrate clock.
        if _can_integrate(
            state,
            integrate=integrate,
            fresh=fresh,
            publication_id=publication_id,
            source_mono=source_mono,
        ):
            state.last_publication_id = publication_id
            if source_mono is not None:
                state.last_source_mono = float(source_mono)
        state.opposite_since_mono = None
        state.zero_cross_holding = False
        state.last_direction = 0
        state.last_proposed_w = 0
        filtered = state.grid_filtered if state.grid_filtered is not None else float(grid_raw_w)
        return PDStepResult(
            proposed_command_w=0,
            filtered_grid_w=filtered,
            error_w=0.0,
            p_term=0.0,
            d_term=0.0,
            in_deadband=False,
            slew_limited=False,
            clamped=False,
            zero_cross_hold=False,
            integrate_applied=False,
            reason_code="operation_blocked",
        )

    if not _can_integrate(
        state,
        integrate=integrate,
        fresh=fresh,
        publication_id=publication_id,
        source_mono=source_mono,
    ):
        if not fresh:
            reason = "stale_input"
        elif not integrate:
            reason = "watchdog"
        else:
            reason = "same_publication"
        return _frozen_result(
            state,
            params,
            grid_raw_w=grid_raw_w,
            reference_command_w=reference_command_w,
            allow_charge=allow_charge,
            allow_discharge=allow_discharge,
            reason_code=reason,
        )

    sample_mono = float(source_mono) if source_mono is not None else float(now_mono)
    if state.last_integrate_mono is None:
        p_dt = NOMINAL_CYCLE_S
    else:
        p_dt = _clamp(sample_mono - state.last_integrate_mono, P_DT_MIN_S, P_DT_MAX_S)
    d_dt = max(p_dt, DERIVATIVE_DT_FLOOR_S)

    filtered = state.grid_filtered
    if filtered is None:
        filtered = float(grid_raw_w)
    else:
        step_threshold = max(3.0 * params.deadband_w, STEP_FLOOR_W)
        if abs(grid_raw_w - filtered) > step_threshold:
            filtered = float(grid_raw_w)
        else:
            filtered = _ema(filtered, float(grid_raw_w), p_dt, params.grid_ema_tau_s)
    state.grid_filtered = filtered

    error = filtered - target_grid_w
    in_deadband = abs(error) <= params.deadband_w
    p_scale = p_dt / NOMINAL_CYCLE_S
    if params.kp > 0:
        # Incremental P behaves like an integrator. Cap it so a slow sample
        # cannot apply an open-loop gain above 1.
        p_scale = min(p_scale, max(1.0, 1.0 / params.kp))

    p_term = 0.0
    d_term = 0.0
    slew_limited = False
    reason: str | None = None
    reference = float(reference_command_w)

    if in_deadband:
        raw_command = reference
        state.derivative_filtered = 0.0
        reason = "pd_deadband"
    else:
        if state.derivative_ready and not state.suppress_derivative_once:
            deriv_raw = (error - state.previous_error) / d_dt
            state.derivative_filtered = _ema(
                state.derivative_filtered,
                deriv_raw,
                d_dt,
                params.derivative_ema_tau_s,
            )
            d_term = params.kd * state.derivative_filtered
        else:
            state.derivative_filtered = 0.0
            d_term = 0.0
        state.derivative_ready = True
        state.suppress_derivative_once = False
        p_term = params.kp * error * p_scale
        raw_command = reference + p_term + d_term
        max_delta = params.max_power_change_w * (p_dt / NOMINAL_CYCLE_S)
        limited = _clamp(raw_command, reference - max_delta, reference + max_delta)
        slew_limited = abs(limited - raw_command) > 0.5
        if slew_limited:
            reason = "pd_slew_limited"
        raw_command = limited

    state.previous_error = error
    state.last_error = error
    state.last_p_term = p_term
    state.last_d_term = d_term
    state.last_slew_limited = slew_limited
    state.last_in_deadband = in_deadband
    state.last_integrate_mono = sample_mono
    state.last_publication_id = publication_id
    state.last_source_mono = sample_mono

    if reference_command_w != 0:
        state.last_direction = _sign(reference_command_w)

    proposed_unclamped = int(round(raw_command))
    proposed, zero_hold, dir_reason = _apply_direction_gate(
        state,
        params,
        proposed_w=proposed_unclamped,
        reference_w=reference_command_w,
        error_w=error,
        now_mono=sample_mono,
    )
    if dir_reason:
        reason = dir_reason

    if proposed > 0 and not allow_discharge:
        proposed = 0
        reason = "discharge_blocked"
    elif proposed < 0 and not allow_charge:
        proposed = 0
        reason = "charge_blocked"

    proposed, clamped = _clamp_command(float(proposed), params)
    if clamped and reason is None:
        reason = "pd_clamped"

    if not zero_hold and proposed != 0:
        # Remember an emitted direction so the next sample does not re-arm the hold
        # while the write that will accept it is still in flight.
        state.last_direction = _sign(proposed)
    elif (
        not zero_hold
        and state.opposite_since_mono is None
        and reference_command_w == 0
        and proposed == 0
    ):
        state.last_direction = 0

    state.zero_cross_holding = zero_hold
    state.last_proposed_w = proposed

    return PDStepResult(
        proposed_command_w=proposed,
        filtered_grid_w=filtered,
        error_w=error,
        p_term=p_term,
        d_term=d_term,
        in_deadband=in_deadband,
        slew_limited=slew_limited,
        clamped=clamped,
        zero_cross_hold=zero_hold,
        integrate_applied=True,
        reason_code=reason,
    )


def control_grid(
    state: PDState,
    params: PDParams,
    tracker: PublicationTracker,
    *,
    now_mono: float,
    reference_command_w: int,
    target_grid_w: float = 0.0,
    force_zero: bool = False,
    allow_charge: bool = True,
    allow_discharge: bool = True,
) -> PDStepResult:
    """Advance from the tracker's grid sample, at most once per publication."""
    value = tracker.grid.value
    eligible = value is not None and tracker.should_integrate_grid(now_mono)
    result = pd_step(
        state,
        params,
        grid_raw_w=0.0 if value is None else float(value),
        now_mono=now_mono,
        source_mono=tracker.grid.source_mono,
        publication_id=tracker.grid.publication_id,
        fresh=value is not None and tracker.grid_is_fresh(now_mono),
        integrate=eligible,
        reference_command_w=reference_command_w,
        target_grid_w=target_grid_w,
        force_zero=force_zero,
        allow_charge=allow_charge,
        allow_discharge=allow_discharge,
    )
    if result.integrate_applied or (force_zero and eligible):
        tracker.mark_grid_integrated()
    return result
