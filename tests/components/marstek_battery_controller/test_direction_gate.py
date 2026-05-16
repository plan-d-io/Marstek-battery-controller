"""Tests for charge/discharge direction gate."""

from __future__ import annotations

from custom_components.marstek_battery_controller import const
from custom_components.marstek_battery_controller.direction_gate import (
    DirectionGateState,
    evaluate_direction_gate,
)


def _apply_sequence(
    mode: str,
    setpoints: list[int],
    *,
    start_target: int = 0,
    times: list[float] | None = None,
) -> list[bool]:
    state = DirectionGateState()
    target = start_target
    applied: list[bool] = []
    for i, sp in enumerate(setpoints):
        t = times[i] if times else float(i)
        result = evaluate_direction_gate(mode, sp, target, state, t)
        applied.append(result.apply)
        if result.apply:
            target = sp
    return applied


def test_bypass_manual_and_released() -> None:
    state = DirectionGateState()
    for mode in (const.MODE_MANUAL, const.MODE_RELEASED):
        for _ in range(5):
            r = evaluate_direction_gate(mode, -500, 500, state, 0.0)
            assert r.apply
        state.flip_times.clear()


def test_two_flips_then_gate_closes() -> None:
    applied = _apply_sequence(
        const.MODE_SELF_CONSUMPTION,
        [500, -300, 400, -200],
        times=[0.0, 1.0, 2.0, 3.0],
    )
    assert applied == [True, True, True, False]


def test_gate_opens_after_window() -> None:
    state = DirectionGateState()
    mode = const.MODE_SELF_CONSUMPTION
    assert evaluate_direction_gate(mode, 500, 0, state, 0.0).apply
    assert evaluate_direction_gate(mode, -300, 500, state, 1.0).apply
    assert evaluate_direction_gate(mode, 400, -300, state, 2.0).apply
    blocked = evaluate_direction_gate(mode, -200, 400, state, 3.0)
    assert not blocked.apply
    assert blocked.reason_code == const.REASON_DIRECTION_COOLDOWN
    # Oldest flip at t=0 drops out of 30s window; only t=2 remains.
    opened = evaluate_direction_gate(mode, -200, 400, state, 31.0)
    assert opened.apply


def test_neutral_not_a_flip_gate_closed_blocks_zero() -> None:
    state = DirectionGateState()
    mode = const.MODE_SELF_CONSUMPTION
    evaluate_direction_gate(mode, 500, 0, state, 0.0)
    evaluate_direction_gate(mode, -300, 500, state, 1.0)
    evaluate_direction_gate(mode, 400, -300, state, 2.0)
    blocked_charge = evaluate_direction_gate(mode, -200, 400, state, 3.0)
    assert not blocked_charge.apply
    blocked_zero = evaluate_direction_gate(mode, 0, 400, state, 4.0)
    assert not blocked_zero.apply


def test_same_side_updates_while_gate_open() -> None:
    state = DirectionGateState()
    mode = const.MODE_SELF_CONSUMPTION
    assert evaluate_direction_gate(mode, 500, 0, state, 0.0).apply
    assert evaluate_direction_gate(mode, 800, 500, state, 1.0).apply
    assert len(state.flip_times) == 0


def test_neutral_to_discharge_not_counted_as_flip() -> None:
    state = DirectionGateState()
    mode = const.MODE_SELF_CONSUMPTION
    evaluate_direction_gate(mode, 0, 500, state, 0.0)
    assert len(state.flip_times) == 0
