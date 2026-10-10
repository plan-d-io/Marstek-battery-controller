"""Deterministic tests for the pure battery PD controller."""

from __future__ import annotations

import pytest

from custom_components.marstek_battery_controller.pd_controller import (
    NOMINAL_CYCLE_S,
    PDParams,
    PDState,
    pd_step,
    reset_pd_state,
    zero_cross_hold_s,
)


def _step(
    state: PDState,
    params: PDParams,
    grid: float,
    t: float,
    reference: int,
    *,
    integrate: bool = True,
    fresh: bool = True,
    publication_id: str | None = None,
    **kwargs: object,
):
    return pd_step(
        state,
        params,
        grid_raw_w=grid,
        now_mono=t,
        source_mono=t,
        reference_command_w=reference,
        integrate=integrate,
        fresh=fresh,
        publication_id=publication_id if publication_id is not None else f"g-{t}",
        **kwargs,  # type: ignore[arg-type]
    )


def test_sign_convention_import_discharges_and_export_charges() -> None:
    """Positive grid is import and must discharge; negative grid must charge.

    This matches production ``modbus_writer`` (positive setpoint → discharge)
    and ``calculator`` (positive grid → positive base, charge commands < 0).
    """
    discharge = _step(PDState(), PDParams(kd=0.0), 1000.0, 0.0, 0)
    charge = _step(PDState(), PDParams(kd=0.0), -1000.0, 0.0, 0)
    assert discharge.proposed_command_w > 0
    assert charge.proposed_command_w < 0
    assert discharge.p_term > 0
    assert charge.p_term < 0


def test_regular_samples_ramp_without_exceeding_step() -> None:
    state = PDState()
    params = PDParams(kp=0.35, kd=0.0, max_power_change_w=200.0)
    reference = 0
    previous = 0
    t = 0.0
    for i in range(6):
        result = _step(state, params, 1000.0, t, reference, publication_id=f"reg-{i}")
        assert result.integrate_applied
        delta = abs(result.proposed_command_w - previous)
        assert delta <= params.max_power_change_w * (NOMINAL_CYCLE_S / NOMINAL_CYCLE_S) + 1
        assert result.proposed_command_w >= previous
        previous = result.proposed_command_w
        reference = result.proposed_command_w
        t += NOMINAL_CYCLE_S
    assert previous > 0


def test_irregular_intervals_keep_a_comparable_ramp() -> None:
    def ramp(dt: float, steps: int) -> int:
        state = PDState()
        params = PDParams(kp=0.35, kd=0.0, max_power_change_w=800.0)
        reference = 0
        t = 0.0
        for i in range(steps):
            result = _step(state, params, 1000.0, t, reference, publication_id=f"{dt}-{i}")
            reference = result.proposed_command_w
            t += dt
        return reference

    fast = ramp(0.5, 8)
    slow = ramp(2.0, 2)
    assert abs(fast - slow) < 400


def test_irregular_step_respects_elapsed_ramp() -> None:
    state = PDState()
    params = PDParams(kp=1.0, kd=0.0, max_power_change_w=100.0, max_battery_power_w=5000.0)
    first = _step(state, params, 2000.0, 0.0, 0, publication_id="i0")
    assert first.proposed_command_w == 100
    second = _step(state, params, 2000.0, 0.4, first.proposed_command_w, publication_id="i1")
    allowed = params.max_power_change_w * (0.4 / NOMINAL_CYCLE_S)
    assert abs(second.proposed_command_w - first.proposed_command_w) <= allowed + 1


def test_same_publication_and_watchdog_do_not_integrate() -> None:
    state = PDState()
    params = PDParams()
    first = _step(state, params, 500.0, 0.0, 0, publication_id="a")
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
            first.proposed_command_w,
            integrate=True,
            publication_id="a",
        )
        assert again.integrate_applied is False
        assert again.reason_code == "same_publication"
        assert again.proposed_command_w == first.proposed_command_w
        assert again.d_term == 0.0
    assert state.grid_filtered == filtered
    assert state.derivative_filtered == derivative
    assert state.last_integrate_mono == integrated_at

    watchdog = _step(
        state,
        params,
        5000.0,
        9.0,
        first.proposed_command_w,
        integrate=False,
        publication_id="a",
    )
    assert watchdog.integrate_applied is False
    assert watchdog.reason_code == "watchdog"
    assert state.last_integrate_mono == integrated_at


def test_stale_and_older_source_are_not_new_feedback() -> None:
    state = PDState()
    params = PDParams()
    _step(state, params, 100.0, 5.0, 0, publication_id="fresh")
    filtered = state.grid_filtered
    stale = _step(
        state,
        params,
        2000.0,
        6.0,
        0,
        fresh=False,
        publication_id="stale-new-id",
    )
    assert stale.integrate_applied is False
    assert stale.reason_code == "stale_input"
    assert state.grid_filtered == filtered
    assert state.last_publication_id == "fresh"

    older = pd_step(
        state,
        params,
        grid_raw_w=2000.0,
        now_mono=7.0,
        source_mono=4.0,
        reference_command_w=0,
        publication_id="older",
        fresh=True,
        integrate=True,
    )
    assert older.integrate_applied is False
    assert state.grid_filtered == filtered


def test_small_change_is_filtered_and_large_step_snaps() -> None:
    state = PDState()
    params = PDParams(deadband_w=40.0, grid_ema_tau_s=2.0, kd=0.0)
    _step(state, params, 0.0, 0.0, 0, publication_id="f0")
    smoothed = _step(state, params, 100.0, 2.0, 0, publication_id="f1")
    assert smoothed.filtered_grid_w == pytest.approx(50.0)

    snapped = PDState()
    _step(snapped, params, 0.0, 0.0, 0, publication_id="s0")
    jumped = _step(snapped, params, 500.0, 2.0, 0, publication_id="s1")
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
        ref_d = 0
        ref_p = 0
        for i, (grid, t) in enumerate(((800.0, 0.0), (800.0, 2.0), (200.0, 4.0))):
            d_result = _step(with_d, params_d, grid, t, ref_d, publication_id=f"d{i}")
            p_result = _step(p_only, params_p, grid, t, ref_p, publication_id=f"p{i}")
            ref_d = d_result.proposed_command_w
            ref_p = p_result.proposed_command_w
        return d_result.d_term, d_result.proposed_command_w, p_result.proposed_command_w

    d_term, with_derivative, proportional_only = drop()
    assert d_term < 0
    assert with_derivative < proportional_only

    rising_d = PDState()
    rising_p = PDState()
    ref_d = 0
    ref_p = 0
    last_d = 0.0
    last_cmd_d = 0
    last_cmd_p = 0
    for i, (grid, t) in enumerate(((200.0, 0.0), (200.0, 2.0), (800.0, 4.0))):
        d_result = _step(rising_d, params_d, grid, t, ref_d, publication_id=f"rd{i}")
        p_result = _step(rising_p, params_p, grid, t, ref_p, publication_id=f"rp{i}")
        ref_d = d_result.proposed_command_w
        ref_p = p_result.proposed_command_w
        last_d = d_result.d_term
        last_cmd_d = d_result.proposed_command_w
        last_cmd_p = p_result.proposed_command_w
    assert last_d > 0
    assert last_cmd_d > last_cmd_p


def test_deadband_holds_command_and_hysteresis_blocks_tiny_starts() -> None:
    state = PDState()
    params = PDParams(kp=0.35, kd=0.0, deadband_w=40.0, direction_hysteresis_w=60.0)
    reference = 0
    t = 0.0
    for i in range(6):
        result = _step(state, params, 400.0, t, reference, publication_id=f"db{i}")
        reference = result.proposed_command_w
        t += 2.0
    assert reference != 0
    held = _step(state, params, 20.0, t, reference, publication_id="hold")
    assert held.in_deadband
    assert held.proposed_command_w == reference
    assert state.derivative_filtered == 0.0

    idle = PDState()
    tiny = _step(idle, params, 50.0, 0.0, 0, publication_id="tiny")
    assert tiny.proposed_command_w == 0
    started = _step(PDState(), params, 200.0, 0.0, 0, publication_id="start")
    assert started.proposed_command_w > 0


def test_output_clamp_and_slew_limit() -> None:
    clamped = _step(
        PDState(),
        PDParams(kp=1.0, kd=0.0, max_power_change_w=10000.0, max_battery_power_w=500.0),
        5000.0,
        0.0,
        0,
        publication_id="clamp",
    )
    assert clamped.proposed_command_w == 500
    assert clamped.clamped
    negative = _step(
        PDState(),
        PDParams(kp=1.0, kd=0.0, max_power_change_w=10000.0, max_battery_power_w=500.0),
        -5000.0,
        0.0,
        0,
        publication_id="clamp-neg",
    )
    assert negative.proposed_command_w == -500

    slewed = _step(
        PDState(),
        PDParams(kp=1.0, kd=0.0, max_power_change_w=100.0, max_battery_power_w=5000.0),
        2000.0,
        0.0,
        0,
        publication_id="slew",
    )
    assert slewed.proposed_command_w == 100
    assert slewed.slew_limited


def test_zero_cross_hold_survives_watchdog_and_then_reverses() -> None:
    state = PDState()
    params = PDParams(direction_hysteresis_w=60.0, battery_actuator_latency_s=2.0, kd=0.0)
    hold_s = zero_cross_hold_s(params)
    assert hold_s >= 5.0

    started = _step(state, params, -800.0, 0.0, 500, publication_id="z0")
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
            0,
            integrate=False,
            publication_id="z0",
        )
        assert watchdog.proposed_command_w == 0
        assert watchdog.zero_cross_hold
        assert state.last_direction == frozen_direction == 1
        assert state.opposite_since_mono == frozen_since == 0.0

    still = _step(state, params, -800.0, hold_s - 0.1, 0, publication_id="z1")
    assert still.proposed_command_w == 0
    assert state.last_direction == 1
    assert state.opposite_since_mono == 0.0

    released = _step(state, params, -800.0, hold_s + 0.1, 0, publication_id="z2")
    assert released.proposed_command_w < 0
    assert released.zero_cross_hold is False


def test_charge_to_discharge_uses_the_same_hold() -> None:
    state = PDState()
    params = PDParams(kd=0.0, battery_actuator_latency_s=2.0)
    hold_s = zero_cross_hold_s(params)
    started = _step(state, params, 900.0, 0.0, -400, publication_id="c0")
    assert started.proposed_command_w == 0
    assert state.last_direction == -1
    early = _step(state, params, 900.0, hold_s - 0.2, 0, publication_id="c1")
    assert early.proposed_command_w == 0
    later = _step(state, params, 900.0, hold_s + 0.2, 0, publication_id="c2")
    assert later.proposed_command_w > 0


def test_transient_reversal_does_not_become_a_stale_flip() -> None:
    state = PDState()
    params = PDParams(direction_hysteresis_w=60.0, kd=0.0)
    hold_s = zero_cross_hold_s(params)
    _step(state, params, -800.0, 0.0, 500, publication_id="t0")
    assert state.opposite_since_mono is not None
    restored = _step(state, params, 400.0, 1.0, 500, publication_id="t1")
    assert state.opposite_since_mono is None
    assert restored.proposed_command_w >= 0

    # One late opposite sample must arm a new hold, not inherit the cancelled one.
    late = _step(state, params, -800.0, 100.0, 500, publication_id="t2")
    assert late.proposed_command_w == 0
    assert late.zero_cross_hold
    assert state.opposite_since_mono == 100.0
    still = _step(state, params, -800.0, 100.0 + hold_s - 0.1, 0, publication_id="t3")
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
    _step(state, params, -2000.0, 0.0, 800, publication_id="r0")
    elapsed = hold_s + 0.1
    reversed_cmd = _step(state, params, -2000.0, elapsed, 0, publication_id="r1")
    assert reversed_cmd.proposed_command_w < 0
    assert reversed_cmd.slew_limited
    allowed = params.max_power_change_w * (elapsed / NOMINAL_CYCLE_S)
    assert abs(reversed_cmd.proposed_command_w) <= allowed + 1


def test_force_zero_consumes_the_publication_without_integrating() -> None:
    state = PDState()
    params = PDParams()
    stopped = _step(
        state, params, 1000.0, 0.0, 0, force_zero=True, publication_id="blocked"
    )
    assert stopped.proposed_command_w == 0
    assert stopped.integrate_applied is False
    assert state.grid_filtered is None
    later = _step(state, params, 1000.0, 2.0, 0, publication_id="blocked")
    assert later.integrate_applied is False
    assert state.grid_filtered is None
    assert state.last_integrate_mono is None


def test_force_zero_clears_a_hold_and_discharge_permission_blocks() -> None:
    state = PDState()
    params = PDParams()
    _step(state, params, -800.0, 0.0, 500, publication_id="b0")
    assert state.opposite_since_mono is not None
    stopped = _step(state, params, -800.0, 1.0, 500, force_zero=True, publication_id="b1")
    assert stopped.proposed_command_w == 0
    assert state.last_direction == 0
    assert state.opposite_since_mono is None

    blocked = _step(
        PDState(),
        params,
        1000.0,
        0.0,
        0,
        allow_discharge=False,
        publication_id="block",
    )
    assert blocked.proposed_command_w == 0
    assert blocked.reason_code == "discharge_blocked"


def test_reset_clears_publication_and_hold_memory() -> None:
    state = PDState()
    _step(state, PDParams(), -800.0, 0.0, 400, publication_id="reset-me")
    assert state.grid_filtered is not None
    assert state.opposite_since_mono is not None
    reset_pd_state(state)
    assert state.grid_filtered is None
    assert state.opposite_since_mono is None
    assert state.last_publication_id is None
    assert state.last_direction == 0
