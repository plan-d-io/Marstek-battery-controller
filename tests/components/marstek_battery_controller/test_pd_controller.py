"""Deterministic tests for the pure battery PD controller."""

from __future__ import annotations

import math

import pytest

from custom_components.marstek_battery_controller.command_ledger import (
    decide_actuator_write,
)
from custom_components.marstek_battery_controller.pd_controller import (
    DEFAULT_DEADBAND_W,
    DEFAULT_MIN_INTERVAL_S,
    NOMINAL_CYCLE_S,
    PD_DEADBAND_MAX_W,
    PD_DEADBAND_MIN_W,
    PD_DEADBAND_STEP_W,
    PDParams,
    PDState,
    control_grid,
    note_accepted_command,
    pd_step,
    reanchor_pd_state,
    release_control,
    reset_pd_state,
    zero_cross_hold_s,
)
from custom_components.marstek_battery_controller.publications import PublicationTracker


def _step(
    state: PDState,
    params: PDParams,
    grid: float,
    t: float,
    *,
    integrate: bool = True,
    fresh: bool = True,
    publication_id: str | None = None,
    source_mono: float | None = None,
    now_mono: float | None = None,
    **kwargs: object,
):
    return pd_step(
        state,
        params,
        grid_raw_w=grid,
        now_mono=t if now_mono is None else now_mono,
        source_mono=t if source_mono is None else source_mono,
        integrate=integrate,
        fresh=fresh,
        publication_id=publication_id if publication_id is not None else f"g-{t}",
        **kwargs,  # type: ignore[arg-type]
    )


def _accept(state: PDState, command_w: int) -> None:
    note_accepted_command(state, command_w)
    state.requested_w = command_w


def _memory(state: PDState) -> tuple[object, ...]:
    return (
        state.grid_filtered,
        state.previous_error,
        state.derivative_filtered,
        state.derivative_ready,
        state.suppress_derivative_once,
        state.last_integrate_mono,
        state.last_publication_id,
        state.last_source_mono,
        state.last_opportunity_mono,
        state.requested_w,
        state.last_direction,
        state.opposite_since_mono,
        state.zero_cross_holding,
        state.reversal_unlocked,
        state.last_error,
        state.last_p_term,
        state.last_d_term,
    )


def test_sign_convention_import_discharges_and_export_charges() -> None:
    """Positive grid is import and must discharge; negative grid must charge.

    This matches production ``modbus_writer`` (positive setpoint → discharge)
    and ``calculator`` (positive grid → positive base, charge commands < 0).
    """
    discharge = _step(PDState(), PDParams(kd=0.0), 1000.0, 0.0)
    charge = _step(PDState(), PDParams(kd=0.0), -1000.0, 0.0)
    assert discharge.proposed_command_w > 0
    assert charge.proposed_command_w < 0
    assert discharge.p_term > 0
    assert charge.p_term < 0


def test_regular_samples_ramp_without_exceeding_step() -> None:
    state = PDState()
    params = PDParams(kp=0.35, kd=0.0, max_power_change_w=200.0)
    previous = 0
    t = 0.0
    for i in range(6):
        result = _step(state, params, 1000.0, t, publication_id=f"reg-{i}")
        assert result.integrate_applied
        delta = abs(result.proposed_command_w - previous)
        allowed = params.max_power_change_w * (
            (params.min_interval_s if i == 0 else NOMINAL_CYCLE_S) / NOMINAL_CYCLE_S
        )
        assert delta <= allowed + 1
        assert result.proposed_command_w >= previous
        previous = result.proposed_command_w
        t += NOMINAL_CYCLE_S
    assert previous > 0


def test_irregular_intervals_keep_a_comparable_ramp() -> None:
    def ramp(dt: float, steps: int) -> int:
        state = PDState()
        params = PDParams(kp=0.35, kd=0.0, max_power_change_w=800.0)
        t = 0.0
        command = 0
        for i in range(steps):
            result = _step(state, params, 1000.0, t, publication_id=f"{dt}-{i}")
            command = result.proposed_command_w
            t += dt
        return command

    fast = ramp(0.5, 8)
    slow = ramp(2.0, 2)
    assert abs(fast - slow) < 400


def test_irregular_step_respects_elapsed_ramp() -> None:
    state = PDState()
    params = PDParams(
        kp=1.0,
        kd=0.0,
        max_power_change_w=100.0,
        max_battery_power_w=5000.0,
        min_interval_s=2.0,
    )
    first = _step(state, params, 2000.0, 0.0, publication_id="i0")
    assert first.proposed_command_w == 100
    second = _step(state, params, 2000.0, 0.4, publication_id="i1")
    allowed = params.max_power_change_w * (0.4 / NOMINAL_CYCLE_S)
    assert abs(second.proposed_command_w - first.proposed_command_w) <= allowed + 1


def test_same_publication_and_watchdog_do_not_integrate() -> None:
    state = PDState()
    params = PDParams()
    first = _step(state, params, 500.0, 0.0, publication_id="a")
    assert first.integrate_applied
    filtered = state.grid_filtered
    derivative = state.derivative_filtered
    integrated_at = state.last_integrate_mono
    for i in range(5):
        again = _step(
            state,
            params,
            5000.0,
            float(i + 1),
            integrate=True,
            publication_id="a",
        )
        assert again.integrate_applied is False
        assert again.reason_code == "same_publication"
        assert again.proposed_command_w == first.proposed_command_w
        assert again.p_term == 0.0
        assert again.d_term == 0.0
    assert state.grid_filtered == filtered
    assert state.derivative_filtered == derivative
    assert state.last_integrate_mono == integrated_at

    watchdog = _step(
        state,
        params,
        5000.0,
        9.0,
        integrate=False,
        publication_id="a",
    )
    assert watchdog.integrate_applied is False
    assert watchdog.reason_code == "watchdog"
    assert watchdog.p_term == 0.0
    assert watchdog.d_term == 0.0
    assert state.last_integrate_mono == integrated_at
    assert state.derivative_filtered == derivative


def test_stale_input_returns_zero_without_advancing_state() -> None:
    state = PDState()
    params = PDParams()
    _accept(state, 0)
    started = _step(state, params, 1000.0, 5.0, publication_id="fresh")
    assert started.proposed_command_w > 0
    before = _memory(state)
    stale = _step(
        state,
        params,
        2000.0,
        6.0,
        fresh=False,
        publication_id="stale-new-id",
    )
    assert stale.integrate_applied is False
    assert stale.reason_code == "stale_input"
    assert stale.proposed_command_w == 0
    assert stale.p_term == 0.0
    assert stale.d_term == 0.0
    assert _memory(state) == before

    older = pd_step(
        state,
        params,
        grid_raw_w=2000.0,
        now_mono=7.0,
        source_mono=4.0,
        publication_id="older",
        fresh=True,
        integrate=True,
    )
    assert older.integrate_applied is False
    assert state.grid_filtered == before[0]
    assert state.requested_w == started.proposed_command_w


def test_small_change_is_filtered_and_large_step_snaps() -> None:
    state = PDState()
    params = PDParams(deadband_w=40.0, grid_ema_tau_s=2.0, kd=0.0)
    _step(state, params, 0.0, 0.0, publication_id="f0")
    smoothed = _step(state, params, 100.0, 2.0, publication_id="f1")
    assert smoothed.filtered_grid_w == pytest.approx(50.0)

    snapped = PDState()
    _step(snapped, params, 0.0, 0.0, publication_id="s0")
    jumped = _step(snapped, params, 500.0, 2.0, publication_id="s1")
    assert jumped.filtered_grid_w == pytest.approx(500.0)


def test_derivative_opposes_a_falling_load_and_assists_a_rising_one() -> None:
    params_d = PDParams(
        kp=0.35,
        kd=1.0,
        derivative_ema_tau_s=0.5,
        max_power_change_w=5000.0,
        deadband_w=40.0,
    )
    params_p = PDParams(
        kp=0.35,
        kd=0.0,
        max_power_change_w=5000.0,
        deadband_w=40.0,
    )

    def drop() -> tuple[float, int, int]:
        with_d = PDState()
        p_only = PDState()
        d_result = p_result = None
        for i, (grid, t) in enumerate(((800.0, 0.0), (800.0, 2.0), (200.0, 4.0))):
            d_result = _step(with_d, params_d, grid, t, publication_id=f"d{i}")
            p_result = _step(p_only, params_p, grid, t, publication_id=f"p{i}")
        assert d_result is not None and p_result is not None
        return d_result.d_term, d_result.proposed_command_w, p_result.proposed_command_w

    d_term, with_derivative, proportional_only = drop()
    assert d_term < 0
    assert with_derivative < proportional_only

    rising_d = PDState()
    rising_p = PDState()
    last_d = 0.0
    last_cmd_d = 0
    last_cmd_p = 0
    for i, (grid, t) in enumerate(((200.0, 0.0), (200.0, 2.0), (800.0, 4.0))):
        d_result = _step(rising_d, params_d, grid, t, publication_id=f"rd{i}")
        p_result = _step(rising_p, params_p, grid, t, publication_id=f"rp{i}")
        last_d = d_result.d_term
        last_cmd_d = d_result.proposed_command_w
        last_cmd_p = p_result.proposed_command_w
    assert last_d > 0
    assert last_cmd_d > last_cmd_p


def test_deadband_holds_command_and_hysteresis_blocks_tiny_starts() -> None:
    state = PDState()
    params = PDParams(kp=0.35, kd=0.0, deadband_w=40.0, direction_hysteresis_w=60.0)
    t = 0.0
    for i in range(6):
        result = _step(state, params, 400.0, t, publication_id=f"db{i}")
        t += 2.0
    assert result.proposed_command_w != 0
    held_command = result.proposed_command_w
    held = _step(state, params, 20.0, t, publication_id="hold")
    assert held.in_deadband
    assert held.proposed_command_w == held_command
    assert state.derivative_filtered == 0.0

    idle = PDState()
    tiny = _step(idle, params, 50.0, 0.0, publication_id="tiny")
    assert tiny.proposed_command_w == 0
    started = _step(PDState(), params, 200.0, 0.0, publication_id="start")
    assert started.proposed_command_w > 0


def test_grid_deadband_range_and_exit_without_a_derivative_kick() -> None:
    """Deadband is grid-error hold, not a write threshold or a delivery tolerance."""
    assert PD_DEADBAND_MIN_W == 0.0
    assert DEFAULT_DEADBAND_W == 40.0
    assert PD_DEADBAND_MAX_W == 200.0
    assert PD_DEADBAND_STEP_W == 5.0
    assert PDParams().deadband_w == DEFAULT_DEADBAND_W

    def hold_at(deadband: float, grid: float) -> tuple[bool, int]:
        state = PDState()
        _accept(state, 300)
        result = _step(
            state,
            PDParams(kp=0.35, kd=1.0, deadband_w=deadband, max_power_change_w=5000.0),
            grid,
            0.0,
            publication_id=f"db-{deadband}-{grid}",
        )
        return result.in_deadband, result.proposed_command_w

    disabled_band, disabled_cmd = hold_at(0.0, 10.0)
    assert disabled_band is False
    assert disabled_cmd != 300

    default_inside, default_held = hold_at(40.0, 20.0)
    assert default_inside is True
    assert default_held == 300
    default_outside, default_moved = hold_at(40.0, 80.0)
    assert default_outside is False
    assert default_moved != 300

    wide_inside, wide_held = hold_at(200.0, 150.0)
    assert wide_inside is True
    assert wide_held == 300
    wide_outside, wide_moved = hold_at(200.0, 400.0)
    assert wide_outside is False
    assert wide_moved != 300

    params = PDParams(
        kp=0.35,
        kd=1.0,
        deadband_w=40.0,
        derivative_ema_tau_s=0.5,
        max_power_change_w=5000.0,
    )
    state = PDState()
    _step(state, params, 400.0, 0.0, publication_id="enter-0")
    built = _step(state, params, 400.0, 2.0, publication_id="enter-1")
    assert built.proposed_command_w != 0
    held_command = built.proposed_command_w
    entered = _step(state, params, 15.0, 4.0, publication_id="inside")
    assert entered.in_deadband
    assert entered.proposed_command_w == held_command
    assert state.derivative_filtered == 0.0
    left = _step(state, params, 400.0, 6.0, publication_id="leave")
    assert left.in_deadband is False
    assert left.d_term == 0.0

    steady = PDState()
    _accept(steady, 300)
    steady_params = PDParams(kd=0.0, deadband_w=40.0)
    for index, stamp in enumerate((0.0, 2.0, 4.0, 6.0)):
        stayed = _step(
            steady,
            steady_params,
            15.0,
            stamp,
            publication_id=f"steady-{index}",
        )
        assert stayed.in_deadband
        assert stayed.proposed_command_w == 300
    for _ in range(5):
        decision = decide_actuator_write(
            requested_w=steady.requested_w,
            reported_command_w=300,
            measured_power_w=80.0,
            measured_fresh=True,
            now_mono=20.0,
            last_change_mono=0.0,
            actuator_latency_s=3.0,
        )
        assert decision.action == "skip"
    assert steady.requested_w == 300


def test_output_clamp_and_slew_limit() -> None:
    clamped = _step(
        PDState(),
        PDParams(kp=1.0, kd=0.0, max_power_change_w=10000.0, max_battery_power_w=500.0),
        5000.0,
        0.0,
        publication_id="clamp",
    )
    assert clamped.proposed_command_w == 500
    assert clamped.clamped
    negative = _step(
        PDState(),
        PDParams(kp=1.0, kd=0.0, max_power_change_w=10000.0, max_battery_power_w=500.0),
        -5000.0,
        0.0,
        publication_id="clamp-neg",
    )
    assert negative.proposed_command_w == -500

    slewed = _step(
        PDState(),
        PDParams(kp=1.0, kd=0.0, max_power_change_w=100.0, max_battery_power_w=5000.0),
        2000.0,
        0.0,
        publication_id="slew",
    )
    first_dt = PDParams().min_interval_s
    assert slewed.proposed_command_w == int(round(100.0 * (first_dt / NOMINAL_CYCLE_S)))
    assert slewed.slew_limited


def test_zero_cross_hold_survives_watchdog_and_then_reverses() -> None:
    state = PDState()
    params = PDParams(direction_hysteresis_w=60.0, battery_actuator_latency_s=2.0, kd=0.0)
    hold_s = zero_cross_hold_s(params)
    assert hold_s >= 5.0
    _accept(state, 500)

    started = _step(state, params, -800.0, 0.0, publication_id="z0")
    assert started.proposed_command_w == 0
    assert started.zero_cross_hold
    assert state.last_direction == 1
    assert state.opposite_since_mono == 0.0

    for stamp in (1.0, hold_s + 10.0):
        frozen_direction = state.last_direction
        frozen_since = state.opposite_since_mono
        watchdog = _step(
            state,
            params,
            -800.0,
            stamp,
            integrate=False,
            publication_id="z0",
        )
        assert watchdog.proposed_command_w == 0
        assert watchdog.zero_cross_hold
        assert state.last_direction == frozen_direction == 1
        assert state.opposite_since_mono == frozen_since == 0.0

    still = _step(state, params, -800.0, hold_s - 0.1, publication_id="z1")
    assert still.proposed_command_w == 0
    assert state.last_direction == 1
    assert state.opposite_since_mono == 0.0

    released = _step(state, params, -800.0, hold_s + 0.1, publication_id="z2")
    assert released.proposed_command_w < 0
    assert released.zero_cross_hold is False
    assert state.last_direction == 1
    assert state.reversal_unlocked

    followed = _step(state, params, -800.0, hold_s + 2.0, publication_id="z3")
    assert followed.zero_cross_hold is False
    assert followed.proposed_command_w < 0


def test_zero_cross_hold_starts_at_receipt_time() -> None:
    """A source timestamp five seconds behind receipt must not age the dwell."""
    state = PDState()
    params = PDParams(kd=0.0, battery_actuator_latency_s=3.0)
    hold_s = zero_cross_hold_s(params)
    assert hold_s == 6.0
    _accept(state, 350)
    _step(state, params, 400.0, 0.0, publication_id="base")
    assert state.last_direction == 1
    assert state.opposite_since_mono is None

    lagged = pd_step(
        state,
        params,
        grid_raw_w=-2000.0,
        now_mono=10.0,
        source_mono=5.0,
        publication_id="lag",
    )
    assert lagged.zero_cross_hold
    assert state.opposite_since_mono == 10.0

    # If the dwell had started at the source time, it would already be over.
    midpoint = pd_step(
        state,
        params,
        grid_raw_w=-2000.0,
        now_mono=11.0,
        source_mono=6.0,
        publication_id="mid",
    )
    assert midpoint.proposed_command_w == 0
    assert midpoint.zero_cross_hold
    assert state.opposite_since_mono == 10.0

    done = pd_step(
        state,
        params,
        grid_raw_w=-2000.0,
        now_mono=10.0 + hold_s + 0.1,
        source_mono=12.0,
        publication_id="done",
    )
    assert done.zero_cross_hold is False
    assert done.proposed_command_w < 0


def test_proposal_does_not_change_physical_direction() -> None:
    missed = PDState()
    params = PDParams(kp=0.35, kd=0.0, max_power_change_w=5000.0)
    proposed = _step(missed, params, 2000.0, 0.0, publication_id="prop")
    assert proposed.proposed_command_w == 350
    assert missed.last_direction == 0
    # The transport never accepted +350 W, so the next charge request is not a reversal.
    charging = _step(missed, params, -2000.0, 2.0, publication_id="charge")
    assert charging.zero_cross_hold is False
    assert charging.proposed_command_w < 0
    assert missed.last_direction == 0

    accepted = PDState()
    _step(accepted, params, 2000.0, 0.0, publication_id="acc")
    note_accepted_command(accepted, 350)
    assert accepted.last_direction == 1
    held = _step(accepted, params, -2000.0, 2.0, publication_id="rev")
    assert held.zero_cross_hold
    assert held.proposed_command_w == 0
    assert accepted.last_direction == 1
    note_accepted_command(accepted, 0)
    assert accepted.last_direction == 1
    release_control(accepted)
    assert accepted.last_direction == 0
    assert accepted.requested_w == 0
    assert accepted.opposite_since_mono is None


def test_charge_to_discharge_uses_the_same_hold() -> None:
    state = PDState()
    params = PDParams(kd=0.0, battery_actuator_latency_s=2.0)
    hold_s = zero_cross_hold_s(params)
    _accept(state, -400)
    started = _step(state, params, 900.0, 0.0, publication_id="c0")
    assert started.proposed_command_w == 0
    assert state.last_direction == -1
    early = _step(state, params, 900.0, hold_s - 0.2, publication_id="c1")
    assert early.proposed_command_w == 0
    later = _step(state, params, 900.0, hold_s + 0.2, publication_id="c2")
    assert later.proposed_command_w > 0


def test_transient_reversal_does_not_become_a_stale_flip() -> None:
    state = PDState()
    params = PDParams(direction_hysteresis_w=60.0, kd=0.0)
    hold_s = zero_cross_hold_s(params)
    _accept(state, 500)
    _step(state, params, -800.0, 0.0, publication_id="t0")
    assert state.opposite_since_mono is not None
    restored = _step(state, params, 400.0, 1.0, publication_id="t1")
    assert state.opposite_since_mono is None
    assert restored.proposed_command_w >= 0
    assert state.reversal_unlocked is False

    late = _step(state, params, -800.0, 100.0, publication_id="t2")
    assert late.proposed_command_w == 0
    assert late.zero_cross_hold
    assert state.opposite_since_mono == 100.0
    still = _step(state, params, -800.0, 100.0 + hold_s - 0.1, publication_id="t3")
    assert still.proposed_command_w == 0


def test_first_reverse_command_is_slew_limited_from_zero() -> None:
    state = PDState()
    params = PDParams(
        kp=1.0,
        kd=0.0,
        max_power_change_w=250.0,
        battery_actuator_latency_s=0.0,
        max_battery_power_w=5000.0,
    )
    hold_s = zero_cross_hold_s(params)
    _accept(state, 800)
    _step(state, params, -2000.0, 0.0, publication_id="r0")
    elapsed = hold_s + 0.1
    reversed_cmd = _step(state, params, -2000.0, elapsed, publication_id="r1")
    assert reversed_cmd.proposed_command_w < 0
    assert reversed_cmd.slew_limited
    allowed = params.max_power_change_w * (elapsed / NOMINAL_CYCLE_S)
    assert abs(reversed_cmd.proposed_command_w) <= allowed + 1


def test_force_zero_consumes_the_publication_without_integrating() -> None:
    state = PDState()
    params = PDParams()
    stopped = _step(
        state, params, 1000.0, 0.0, force_zero=True, publication_id="blocked"
    )
    assert stopped.proposed_command_w == 0
    assert stopped.integrate_applied is False
    assert state.grid_filtered is None
    later = _step(state, params, 1000.0, 2.0, publication_id="blocked")
    assert later.integrate_applied is False
    assert state.grid_filtered is None
    assert state.last_integrate_mono is None


def test_force_zero_clears_a_hold_and_discharge_permission_blocks() -> None:
    state = PDState()
    params = PDParams()
    _accept(state, 500)
    _step(state, params, -800.0, 0.0, publication_id="b0")
    assert state.opposite_since_mono is not None
    stopped = _step(state, params, -800.0, 1.0, force_zero=True, publication_id="b1")
    assert stopped.proposed_command_w == 0
    assert state.last_direction == 0
    assert state.opposite_since_mono is None
    assert state.requested_w == 0

    blocked = _step(
        PDState(),
        params,
        1000.0,
        0.0,
        allow_discharge=False,
        publication_id="block",
    )
    assert blocked.proposed_command_w == 0
    assert blocked.reason_code == "discharge_blocked"


def test_permission_block_bypasses_the_interval_without_integrating() -> None:
    state = PDState()
    params = PDParams(min_interval_s=5.0)
    tracker = PublicationTracker()
    tracker.publish("grid", 400.0, source_mono=0.0, receive_mono=0.0, publication_id="g0")
    first = control_grid(state, params, tracker, now_mono=0.0)
    assert first.integrate_applied
    assert state.requested_w > 0
    filtered = state.grid_filtered
    integrated = state.last_integrate_mono
    tracker.publish("grid", 900.0, source_mono=0.2, receive_mono=0.2, publication_id="g1")
    blocked = control_grid(
        state,
        params,
        tracker,
        now_mono=0.2,
        allow_discharge=False,
    )
    assert blocked.proposed_command_w == 0
    assert blocked.reason_code == "discharge_blocked"
    assert blocked.integrate_applied is False
    assert state.requested_w == 0
    assert state.last_opportunity_mono == 0.0
    assert state.grid_filtered == filtered
    assert state.last_integrate_mono == integrated
    assert tracker.should_integrate_grid(0.2)


def test_closed_gate_keeps_the_newest_sample_for_the_next_opportunity() -> None:
    state = PDState()
    params = PDParams(kp=0.35, kd=0.0, min_interval_s=5.0, max_power_change_w=5000.0)
    tracker = PublicationTracker()
    tracker.publish("grid", 100.0, source_mono=0.0, receive_mono=0.0, publication_id="fast-0")
    opened = control_grid(state, params, tracker, now_mono=0.0)
    assert opened.integrate_applied
    frozen = state.requested_w
    for index, stamp in enumerate((0.5, 1.0, 2.0, 3.5, 4.5), start=1):
        tracker.publish(
            "grid",
            2000.0,
            source_mono=stamp,
            receive_mono=stamp,
            publication_id=f"fast-{index}",
        )
        closed = control_grid(state, params, tracker, now_mono=stamp)
        assert closed.integrate_applied is False
        assert closed.reason_code == "interval_gate"
        assert closed.p_term == 0.0
        assert state.requested_w == frozen
    tracker.publish("grid", 2000.0, source_mono=5.0, receive_mono=5.0, publication_id="open")
    nxt = control_grid(state, params, tracker, now_mono=5.0)
    assert nxt.integrate_applied
    # One 5 s step from the newest 2000 W sample, not a replaced 0.5 s step.
    assert state.requested_w - frozen > 1000


def test_reset_clears_publication_and_hold_memory() -> None:
    state = PDState()
    _accept(state, 400)
    _step(state, PDParams(), -800.0, 0.0, publication_id="reset-me")
    assert state.grid_filtered is not None
    assert state.opposite_since_mono is not None
    reset_pd_state(state)
    assert state.grid_filtered is None
    assert state.opposite_since_mono is None
    assert state.last_publication_id is None
    assert state.last_direction == 0
    assert state.requested_w == 0
    assert state.last_opportunity_mono is None


def test_min_interval_default_is_independent_of_the_five_second_entity() -> None:
    assert DEFAULT_MIN_INTERVAL_S == 1.0
    assert PDParams().min_interval_s == 1.0


def test_sub_floor_requests_snap_to_zero() -> None:
    """1–24 W cannot satisfy the 25 W delivery floor, so the request is idle."""
    params = PDParams(kd=0.0, deadband_w=40.0, direction_hysteresis_w=0.0)
    for watts in range(1, 25):
        for sign in (1, -1):
            state = PDState()
            state.requested_w = sign * watts
            result = pd_step(
                state,
                params,
                grid_raw_w=0.0,
                now_mono=0.0,
                source_mono=0.0,
                publication_id=f"floor-{sign}-{watts}",
            )
            assert result.proposed_command_w == 0
            assert result.reason_code == "pd_noise_floor"
            assert state.requested_w == 0

    for kept in (25, -25):
        state = PDState()
        state.requested_w = kept
        result = pd_step(
            state,
            params,
            grid_raw_w=0.0,
            now_mono=0.0,
            source_mono=0.0,
            publication_id=f"keep-{kept}",
        )
        assert result.proposed_command_w == kept
        assert state.requested_w == kept


def test_reanchor_below_the_noise_floor_is_idle() -> None:
    for watts in range(1, 25):
        for sign in (1, -1):
            state = PDState()
            state.last_direction = -sign
            reanchor_pd_state(state, sign * watts)
            assert state.requested_w == 0
            assert state.last_direction == 0
    for kept in (25, -25):
        state = PDState()
        state.last_direction = 0
        reanchor_pd_state(state, kept)
        assert state.requested_w == kept
        assert state.last_direction == (1 if kept > 0 else -1)


def test_proportional_open_loop_gain_stays_at_most_one() -> None:
    """kp * p_scale stays at most 1, including when kp itself is above 1."""

    def p_term(kp: float, dt: float) -> float:
        state = PDState()
        state.last_integrate_mono = 0.0
        params = PDParams(
            kp=kp,
            kd=0.0,
            deadband_w=0.0,
            direction_hysteresis_w=0.0,
            max_power_change_w=1_000_000.0,
            max_battery_power_w=1_000_000.0,
        )
        result = pd_step(
            state,
            params,
            grid_raw_w=1000.0,
            now_mono=dt,
            source_mono=dt,
            publication_id=f"kp-{kp}-{dt}",
        )
        return result.p_term

    # A 10 s gap at kp = 1 used to be eligible for scale 5. Gain stays 1.
    assert p_term(1.0, 10.0) == pytest.approx(1000.0)
    # kp = 2 must not apply a gain of 2. The old cap max(1, 1/kp) did.
    assert p_term(2.0, 10.0) == pytest.approx(1000.0)
    # A short step already below 1/kp is left alone: 2 * 1000 * (0.4 / 2) = 400.
    assert p_term(2.0, 0.4) == pytest.approx(400.0)
    # Nominal step at the kp = 1 boundary still applies the full error.
    assert p_term(1.0, 2.0) == pytest.approx(1000.0)


@pytest.mark.parametrize(
    "kp",
    [-0.01, -1.0, float("nan"), float("inf"), float("-inf")],
)
def test_kp_must_be_finite_and_nonnegative(kp: float) -> None:
    state = PDState()
    with pytest.raises(ValueError, match="finite and nonnegative"):
        pd_step(
            state,
            PDParams(kp=kp),
            grid_raw_w=500.0,
            now_mono=0.0,
            source_mono=0.0,
            publication_id="bad-kp",
        )
    assert state.grid_filtered is None
    assert state.requested_w == 0


@pytest.mark.parametrize(
    "kp",
    [-1.0, float("nan"), float("inf"), float("-inf")],
)
def test_force_zero_releases_before_kp_validation(kp: float) -> None:
    state = PDState()
    state.requested_w = 400
    state.last_direction = 1
    state.opposite_since_mono = 1.0
    state.zero_cross_holding = True
    result = pd_step(
        state,
        PDParams(kp=kp),
        grid_raw_w=500.0,
        now_mono=2.0,
        source_mono=2.0,
        publication_id="safety",
        force_zero=True,
    )
    assert result.proposed_command_w == 0
    assert result.p_term == 0.0
    assert result.reason_code == "operation_blocked"
    assert state.requested_w == 0
    assert state.last_proposed_w == 0
    assert state.last_direction == 0
    assert state.opposite_since_mono is None
    assert state.zero_cross_holding is False


def test_huge_finite_kp_stays_inside_the_open_loop_cap() -> None:
    state = PDState()
    state.last_integrate_mono = 0.0
    result = pd_step(
        state,
        PDParams(
            kp=1e308,
            kd=0.0,
            deadband_w=0.0,
            direction_hysteresis_w=0.0,
            max_power_change_w=1_000_000.0,
            max_battery_power_w=1_000_000.0,
        ),
        grid_raw_w=1000.0,
        now_mono=10.0,
        source_mono=10.0,
        publication_id="huge-kp",
    )
    assert math.isfinite(result.p_term)
    assert result.p_term == pytest.approx(1000.0)
    assert result.proposed_command_w == 1000


def test_kp_zero_adds_no_proportional_correction() -> None:
    state = PDState()
    state.requested_w = 200
    result = pd_step(
        state,
        PDParams(
            kp=0.0,
            kd=0.0,
            deadband_w=0.0,
            direction_hysteresis_w=0.0,
            max_power_change_w=5000.0,
        ),
        grid_raw_w=1000.0,
        now_mono=0.0,
        source_mono=0.0,
        publication_id="kp0",
    )
    assert result.p_term == 0.0
    assert result.proposed_command_w == 200
    assert state.requested_w == 200
