"""Pure time-aware PD battery controller.

No Home Assistant imports. Callers inject monotonic timestamps.

Seven different things are kept apart:

* a fresh grid publication
* an eligible control opportunity, after ``min_interval_s``
* ``requested_w``, the controller's internal output
* the last command a transport call actually accepted (the ledger)
* the command the device reports (injected later as ``reported_command_w``)
* measured battery power, which is delivery feedback only
* one in-flight physical write

Sign convention (production Marstek, not a guess):

* Positive watts are discharge / grid import.
* Negative watts are charge / grid export.
* Zero is idle / no exchange.

``modbus_writer.async_send_setpoint`` maps ``setpoint_w > 0`` to force discharge
and ``setpoint_w < 0`` to force charge. ``calculator`` agrees: a positive grid
reading produces a positive (discharge) base, boost and manual charge commands
are negative, and the SoC floor blocks a positive setpoint.

On each eligible opportunity the internal request moves by::

    error = filtered_grid - target_grid
    requested += kp * error * (dt / nominal) + kd * filtered_derivative

``dt`` is the source-time gap since the previous opportunity. The first
opportunity has no measured gap and uses ``min_interval_s`` instead, so the
math is not tied to the Home Assistant send-interval default of 5 s.
Positive error (import) increases discharge. Omnibattery uses the opposite
actuator sign and subtracts the same correction.

A calculated request is not a physical command. Direction changes only when a
write is accepted, a measurement re-anchor is applied, or control is released.
The zero-cross dwell starts at ``now_mono``, the time the controller sees the
reversal, never at a source timestamp that is already in the past.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from .const import DEFAULT_MAX_BATTERY_POWER
from .non_delivery import snap_noise_floor_command
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
# Home Assistant controls in this milestone.
DEFAULT_KP: Final = 0.35
DEFAULT_KD: Final = 0.30
# Allowed grid error around the target. 0 disables it. Milestone 2 exposes
# this as number.marstek_battery_controller_pd_grid_deadband, step 5 W.
PD_DEADBAND_MIN_W: Final = 0.0
PD_DEADBAND_MAX_W: Final = 200.0
PD_DEADBAND_STEP_W: Final = 5.0
DEFAULT_DEADBAND_W: Final = 40.0
DEFAULT_MAX_POWER_CHANGE_W: Final = 800.0
DEFAULT_DIRECTION_HYSTERESIS_W: Final = 60.0
DEFAULT_GRID_EMA_TAU_S: Final = 2.0
DEFAULT_DERIVATIVE_EMA_TAU_S: Final = 3.0
# Longer than Omnibattery's direct Marstek latency (0.8 s) because this
# integration reaches the battery through several Home Assistant service calls.
DEFAULT_ACTUATOR_LATENCY_S: Final = 3.0
# Omnibattery's event-driven PD minimum. The Home Assistant send-interval
# entity (default 5 s, range 1–60 s) is applied later by passing that value
# in. It is not a heartbeat and it is not hard-coded here.
DEFAULT_MIN_INTERVAL_S: Final = 1.0


@dataclass
class PDParams:
    """Controller parameters. Defaults are internal until a later milestone."""

    kp: float = DEFAULT_KP
    kd: float = DEFAULT_KD
    max_power_change_w: float = DEFAULT_MAX_POWER_CHANGE_W
    deadband_w: float = DEFAULT_DEADBAND_W
    direction_hysteresis_w: float = DEFAULT_DIRECTION_HYSTERESIS_W
    grid_ema_tau_s: float = DEFAULT_GRID_EMA_TAU_S
    derivative_ema_tau_s: float = DEFAULT_DERIVATIVE_EMA_TAU_S
    battery_actuator_latency_s: float = DEFAULT_ACTUATOR_LATENCY_S
    max_battery_power_w: float = DEFAULT_MAX_BATTERY_POWER
    # Minimum time between ordinary control opportunities. Not a write period.
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S


@dataclass
class PDState:
    """Filter memory, the internal request, and physical direction."""

    grid_filtered: float | None = None
    previous_error: float = 0.0
    derivative_filtered: float = 0.0
    derivative_ready: bool = False
    suppress_derivative_once: bool = False
    last_integrate_mono: float | None = None
    last_publication_id: str | None = None
    last_source_mono: float | None = None
    # Receipt time of the last ordinary control opportunity.
    last_opportunity_mono: float | None = None
    # Internal PD output. Distinct from the last accepted physical command.
    requested_w: int = 0
    # Last nonzero physical direction: +1 discharge, -1 charge, 0 idle.
    last_direction: int = 0
    opposite_since_mono: float | None = None
    zero_cross_holding: bool = False
    # Set when a hold elapses so the next sample can cross without a new dwell.
    reversal_unlocked: bool = False
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


def grid_deadband_w(params: PDParams) -> float:
    """Allowed grid error. Values outside 0–200 W are pulled into that range."""
    return _clamp(float(params.deadband_w), PD_DEADBAND_MIN_W, PD_DEADBAND_MAX_W)


def _ema(previous: float, raw: float, dt: float, tau: float) -> float:
    alpha = dt / (tau + dt) if tau > 0 else 1.0
    return previous + alpha * (raw - previous)


def zero_cross_hold_s(params: PDParams) -> float:
    """How long an opposite-direction demand must persist before a reversal."""
    return max(DEFAULT_ZERO_CROSS_HOLD_S, 2.0 * params.battery_actuator_latency_s)


def reset_pd_state(state: PDState) -> None:
    """Clear filter, request, publication, and zero-cross memory."""
    state.grid_filtered = None
    state.previous_error = 0.0
    state.derivative_filtered = 0.0
    state.derivative_ready = False
    state.suppress_derivative_once = False
    state.last_integrate_mono = None
    state.last_publication_id = None
    state.last_source_mono = None
    state.last_opportunity_mono = None
    state.requested_w = 0
    state.last_direction = 0
    state.opposite_since_mono = None
    state.zero_cross_holding = False
    state.reversal_unlocked = False
    state.last_error = 0.0
    state.last_p_term = 0.0
    state.last_d_term = 0.0
    state.last_slew_limited = False
    state.last_in_deadband = False
    state.last_proposed_w = None


def require_kp(kp: float) -> None:
    """Reject a proportional gain that is negative or not finite.

    Zero is valid and adds no proportional correction. Gains above 1 stay
    valid; one sample is still capped at an open-loop gain of 1.
    """
    try:
        gain = float(kp)
    except (TypeError, ValueError) as err:
        raise ValueError(f"kp must be finite and nonnegative, got {kp!r}") from err
    if not math.isfinite(gain) or gain < 0.0:
        raise ValueError(f"kp must be finite and nonnegative, got {kp!r}")


def _proportional_gain(kp: float, p_scale: float) -> float:
    """Return ``min(kp * p_scale, 1)`` without an overflowing product.

    One sample cannot apply an open-loop gain above 1. Comparing ``kp`` with
    ``1 / p_scale`` decides the cap before any multiplication, so a huge
    finite ``kp`` cannot overflow ``kp * error`` on the way there. A step
    whose uncapped product is already below 1 is left unchanged.
    """
    if kp <= 0.0 or p_scale <= 0.0:
        return 0.0
    if kp >= 1.0 / p_scale:
        return 1.0
    return kp * p_scale


def note_accepted_command(state: PDState, command_w: int) -> None:
    """Remember direction from a command the transport actually accepted.

    A proposal never calls this. Accepting zero during a reversal hold keeps
    the last nonzero direction until the reversal finishes or is released.
    A command of 1–24 W is normalized to zero before the direction is taken.
    """
    command = snap_noise_floor_command(int(command_w))
    if command > 0:
        state.last_direction = 1
        state.reversal_unlocked = False
        return
    if command < 0:
        state.last_direction = -1
        state.reversal_unlocked = False
        return
    if state.opposite_since_mono is not None or state.reversal_unlocked:
        return
    state.last_direction = 0


def release_control(state: PDState) -> None:
    """Explicit safety release. Clears the request, the hold, and direction."""
    state.requested_w = 0
    state.last_proposed_w = 0
    state.last_direction = 0
    state.opposite_since_mono = None
    state.zero_cross_holding = False
    state.reversal_unlocked = False


def reanchor_pd_state(state: PDState, measured_power_w: float) -> None:
    """Move the internal request to measured power after sustained non-delivery.

    This does not accept the measurement and does not touch the ledger.
    A measurement inside the 25 W noise floor becomes an idle request.
    """
    measured = snap_noise_floor_command(int(round(measured_power_w)))
    state.previous_error = 0.0
    state.derivative_filtered = 0.0
    state.suppress_derivative_once = True
    state.opposite_since_mono = None
    state.zero_cross_holding = False
    state.reversal_unlocked = False
    state.last_direction = _sign(measured)
    state.last_slew_limited = False
    state.requested_w = measured
    state.last_proposed_w = measured


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


def _read_only_result(
    state: PDState,
    params: PDParams,
    *,
    grid_raw_w: float,
    proposed: int,
    reason_code: str,
    zero_cross_hold: bool,
) -> PDStepResult:
    """Return a proposal without writing controller memory."""
    filtered = state.grid_filtered if state.grid_filtered is not None else float(grid_raw_w)
    limited, clamped = _clamp_command(float(snap_noise_floor_command(proposed)), params)
    return PDStepResult(
        proposed_command_w=limited,
        filtered_grid_w=filtered,
        error_w=state.last_error,
        p_term=0.0,
        d_term=0.0,
        in_deadband=state.last_in_deadband,
        slew_limited=False,
        clamped=clamped,
        zero_cross_hold=zero_cross_hold,
        integrate_applied=False,
        reason_code=reason_code,
    )


def _apply_direction_gate(
    state: PDState,
    params: PDParams,
    *,
    proposed_w: int,
    error_w: float,
    now_mono: float,
) -> tuple[int, bool, str | None]:
    """Hold at zero until an opposite physical direction has persisted.

    ``now_mono`` is the receipt time. A source timestamp must not start the
    dwell in the past. ``last_direction`` is physical and is not modified here.
    """
    direction = state.last_direction
    deadband = grid_deadband_w(params)
    intent_dir = _sign(error_w) if abs(error_w) > deadband else 0
    hold_needed = zero_cross_hold_s(params)

    if state.reversal_unlocked:
        if direction == 0 or intent_dir in (0, direction):
            state.reversal_unlocked = False
        else:
            state.opposite_since_mono = None
            return proposed_w, False, None

    if direction != 0 and intent_dir in (0, direction):
        state.opposite_since_mono = None
        return proposed_w, False, None

    if direction == 0:
        if (
            abs(proposed_w) < params.direction_hysteresis_w
            and abs(error_w) < params.direction_hysteresis_w
        ):
            return 0, False, None
        state.opposite_since_mono = None
        return proposed_w, False, None

    if abs(error_w) < params.direction_hysteresis_w:
        if _sign(proposed_w) in (0, direction):
            return proposed_w, False, None
        return state.requested_w, False, None

    if state.opposite_since_mono is None:
        state.opposite_since_mono = float(now_mono)
        return 0, True, "pd_zero_cross_hold"

    if now_mono - state.opposite_since_mono < hold_needed:
        return 0, True, "pd_zero_cross_hold"

    state.opposite_since_mono = None
    state.reversal_unlocked = True
    if _sign(proposed_w) == direction:
        return 0, False, None
    return proposed_w, False, None


def pd_step(
    state: PDState,
    params: PDParams,
    *,
    grid_raw_w: float,
    now_mono: float,
    integrate: bool = True,
    fresh: bool = True,
    publication_id: str | None = None,
    source_mono: float | None = None,
    target_grid_w: float = 0.0,
    force_zero: bool = False,
    allow_charge: bool = True,
    allow_discharge: bool = True,
) -> PDStepResult:
    """Propose the next internal request from one grid observation.

    The baseline is ``state.requested_w``. The returned proposal is not
    accepted state, and it does not change ``last_direction``.

    A safety release (``force_zero``) runs before gain validation. An invalid
    ``kp`` must not block that release. Ordinary calculations still reject it.
    """
    if force_zero:
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
        release_control(state)
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

    require_kp(params.kp)
    if not fresh:
        return _read_only_result(
            state,
            params,
            grid_raw_w=grid_raw_w,
            proposed=0,
            reason_code="stale_input",
            zero_cross_hold=state.opposite_since_mono is not None,
        )

    if not _can_integrate(
        state,
        integrate=integrate,
        fresh=fresh,
        publication_id=publication_id,
        source_mono=source_mono,
    ):
        reason = "watchdog" if not integrate else "same_publication"
        holding = state.opposite_since_mono is not None
        proposed = 0 if holding else state.requested_w
        return _read_only_result(
            state,
            params,
            grid_raw_w=grid_raw_w,
            proposed=proposed,
            reason_code=reason,
            zero_cross_hold=holding,
        )

    sample_mono = float(source_mono) if source_mono is not None else float(now_mono)
    if state.last_integrate_mono is None:
        p_dt = _clamp(params.min_interval_s, P_DT_MIN_S, P_DT_MAX_S)
    else:
        p_dt = _clamp(sample_mono - state.last_integrate_mono, P_DT_MIN_S, P_DT_MAX_S)
    d_dt = max(p_dt, DERIVATIVE_DT_FLOOR_S)
    deadband = grid_deadband_w(params)

    filtered = state.grid_filtered
    if filtered is None:
        filtered = float(grid_raw_w)
    else:
        step_threshold = max(3.0 * deadband, STEP_FLOOR_W)
        if abs(grid_raw_w - filtered) > step_threshold:
            filtered = float(grid_raw_w)
        else:
            filtered = _ema(filtered, float(grid_raw_w), p_dt, params.grid_ema_tau_s)
    state.grid_filtered = filtered

    error = filtered - target_grid_w
    in_deadband = abs(error) <= deadband
    # p_term = effective_gain * error, effective_gain = min(kp * p_scale, 1).
    gain = _proportional_gain(params.kp, p_dt / NOMINAL_CYCLE_S)

    p_term = 0.0
    d_term = 0.0
    slew_limited = False
    reason: str | None = None
    reference = float(state.requested_w)

    if in_deadband:
        raw_command = reference
        state.derivative_filtered = 0.0
        state.suppress_derivative_once = True
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
        p_term = gain * error
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

    proposed_unclamped = int(round(raw_command))
    proposed, zero_hold, dir_reason = _apply_direction_gate(
        state,
        params,
        proposed_w=proposed_unclamped,
        error_w=error,
        now_mono=now_mono,
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
    floored = snap_noise_floor_command(proposed)
    if floored != proposed:
        proposed = floored
        reason = "pd_noise_floor"

    state.zero_cross_holding = zero_hold
    state.requested_w = proposed
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


def _permission_block(
    state: PDState,
    params: PDParams,
    *,
    grid_raw_w: float,
    allow_charge: bool,
    allow_discharge: bool,
) -> PDStepResult | None:
    """Zero a request that a new SoC or permission block no longer allows.

    Filters, publication identity, cadence, direction, and the zero-cross
    timer stay as they are. The ordinary interval does not delay this.
    """
    if state.requested_w > 0 and not allow_discharge:
        reason = "discharge_blocked"
    elif state.requested_w < 0 and not allow_charge:
        reason = "charge_blocked"
    else:
        return None
    state.requested_w = 0
    state.last_proposed_w = 0
    filtered = state.grid_filtered if state.grid_filtered is not None else float(grid_raw_w)
    return PDStepResult(
        proposed_command_w=0,
        filtered_grid_w=filtered,
        error_w=state.last_error,
        p_term=0.0,
        d_term=0.0,
        in_deadband=state.last_in_deadband,
        slew_limited=False,
        clamped=False,
        zero_cross_hold=state.opposite_since_mono is not None,
        integrate_applied=False,
        reason_code=reason,
    )


def control_grid(
    state: PDState,
    params: PDParams,
    tracker: PublicationTracker,
    *,
    now_mono: float,
    opportunity: bool = True,
    target_grid_w: float = 0.0,
    force_zero: bool = False,
    allow_charge: bool = True,
    allow_discharge: bool = True,
) -> PDStepResult:
    """Run one control observation against the tracker's newest grid sample.

    A fresh publication only integrates when ``opportunity`` is set and the
    minimum interval since the previous opportunity has elapsed. Samples that
    arrive while the gate is closed stay available, so the next open
    opportunity uses the newest one. A watchdog (``opportunity`` false) may
    repeat the current request or apply a safety block. It does not integrate.
    Definitive staleness returns zero and writes nothing.
    """
    value = tracker.grid.value
    fresh = value is not None and tracker.grid_is_fresh(now_mono)
    grid = 0.0 if value is None else float(value)
    source = tracker.grid.source_mono
    publication_id = tracker.grid.publication_id
    eligible = tracker.should_integrate_grid(now_mono)

    if force_zero:
        result = pd_step(
            state,
            params,
            grid_raw_w=grid,
            now_mono=now_mono,
            source_mono=source,
            publication_id=publication_id,
            fresh=fresh,
            integrate=eligible,
            target_grid_w=target_grid_w,
            force_zero=True,
            allow_charge=allow_charge,
            allow_discharge=allow_discharge,
        )
        if eligible:
            tracker.mark_grid_integrated()
        return result

    if not fresh:
        return pd_step(
            state,
            params,
            grid_raw_w=grid,
            now_mono=now_mono,
            source_mono=source,
            publication_id=publication_id,
            fresh=False,
            integrate=False,
            target_grid_w=target_grid_w,
            allow_charge=allow_charge,
            allow_discharge=allow_discharge,
        )

    blocked = _permission_block(
        state,
        params,
        grid_raw_w=grid,
        allow_charge=allow_charge,
        allow_discharge=allow_discharge,
    )
    if blocked is not None:
        return blocked

    gate_open = (
        state.last_opportunity_mono is None
        or (now_mono - state.last_opportunity_mono) + 1e-9 >= params.min_interval_s
    )
    integrate = bool(opportunity and eligible and gate_open)
    if opportunity and eligible and not gate_open:
        holding = state.opposite_since_mono is not None
        return _read_only_result(
            state,
            params,
            grid_raw_w=grid,
            proposed=0 if holding else state.requested_w,
            reason_code="interval_gate",
            zero_cross_hold=holding,
        )

    result = pd_step(
        state,
        params,
        grid_raw_w=grid,
        now_mono=now_mono,
        source_mono=source,
        publication_id=publication_id,
        fresh=True,
        integrate=integrate,
        target_grid_w=target_grid_w,
        allow_charge=allow_charge,
        allow_discharge=allow_discharge,
    )
    if result.integrate_applied:
        state.last_opportunity_mono = float(now_mono)
        tracker.mark_grid_integrated()
    return result
