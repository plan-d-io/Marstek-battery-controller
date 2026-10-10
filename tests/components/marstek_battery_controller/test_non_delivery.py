"""Non-delivery detection and measurement re-anchor."""

from __future__ import annotations

from custom_components.marstek_battery_controller.command_ledger import (
    CommandLedger,
    consider_battery_delivery,
)
from custom_components.marstek_battery_controller.non_delivery import (
    NonDeliveryState,
    evaluate_non_delivery,
)
from custom_components.marstek_battery_controller.pd_controller import (
    PDParams,
    PDState,
    pd_step,
)


def test_ignores_shortfall_during_actuator_delay() -> None:
    state = NonDeliveryState()
    result = evaluate_non_delivery(
        state,
        accepted_command_w=1000,
        measured_power_w=0.0,
        measured_fresh=True,
        now_mono=2.9,
        last_write_mono=0.0,
        actuator_latency_s=3.0,
        publication_id="early",
    )
    assert result.reanchor is False
    assert state.shortfall_count == 0


def test_reanchors_on_fresh_distinct_publications_only() -> None:
    state = NonDeliveryState()
    base = dict(
        accepted_command_w=1000,
        measured_power_w=40.0,
        measured_fresh=True,
        last_write_mono=0.0,
        actuator_latency_s=1.0,
        consecutive_required=3,
        fallback_timeout_s=60.0,
    )
    assert evaluate_non_delivery(state, now_mono=2.0, publication_id="p1", **base).reanchor is False
    assert evaluate_non_delivery(state, now_mono=9.0, publication_id="p1", **base).reanchor is False
    assert state.shortfall_count == 1
    stale = evaluate_non_delivery(
        state,
        now_mono=3.0,
        publication_id="stale",
        accepted_command_w=1000,
        measured_power_w=0.0,
        measured_fresh=False,
        last_write_mono=0.0,
        actuator_latency_s=1.0,
    )
    assert stale.reanchor is False
    assert state.shortfall_count == 1
    assert evaluate_non_delivery(state, now_mono=4.0, publication_id="p2", **base).reanchor is False
    result = evaluate_non_delivery(state, now_mono=5.0, publication_id="p3", **base)
    assert result.reanchor
    assert result.measured_w == 40


def test_one_battery_sample_counts_once_across_watchdogs() -> None:
    state = NonDeliveryState()
    kwargs = dict(
        accepted_command_w=1000,
        measured_power_w=40.0,
        measured_fresh=True,
        last_write_mono=0.0,
        actuator_latency_s=1.0,
        consecutive_required=3,
        fallback_timeout_s=60.0,
    )
    for stamp in (2.0, 3.0, 4.0, 8.0, 12.0):
        evaluate_non_delivery(
            state,
            now_mono=stamp,
            publication_id="same-battery",
            source_mono=2.0,
            **kwargs,
        )
    assert state.shortfall_count == 1

    anonymous = NonDeliveryState()
    for stamp in (2.0, 3.0, 4.0):
        evaluate_non_delivery(
            anonymous,
            now_mono=stamp,
            publication_id=None,
            source_mono=None,
            **kwargs,
        )
    assert anonymous.shortfall_count == 0

    by_time = NonDeliveryState()
    evaluate_non_delivery(
        by_time, now_mono=2.0, publication_id=None, source_mono=2.0, **kwargs
    )
    evaluate_non_delivery(
        by_time, now_mono=9.0, publication_id=None, source_mono=2.0, **kwargs
    )
    assert by_time.shortfall_count == 1


def test_three_distinct_battery_publications_reanchor() -> None:
    state = NonDeliveryState()
    kwargs = dict(
        accepted_command_w=1000,
        measured_power_w=40.0,
        measured_fresh=True,
        last_write_mono=0.0,
        actuator_latency_s=1.0,
        consecutive_required=3,
        fallback_timeout_s=60.0,
    )
    for index, source in enumerate((2.0, 4.0, 6.0), start=1):
        result = evaluate_non_delivery(
            state,
            now_mono=source,
            publication_id=None,
            source_mono=source,
            **kwargs,
        )
    assert state.shortfall_count == 0
    assert result.reanchor
    assert result.measured_w == 40
    assert index == 3


def test_persistent_opposite_delivery_reanchors_without_one_glitch() -> None:
    state = NonDeliveryState()
    kwargs = dict(
        accepted_command_w=1000,
        measured_power_w=-400.0,
        measured_fresh=True,
        last_write_mono=0.0,
        actuator_latency_s=1.0,
        consecutive_required=3,
        fallback_timeout_s=60.0,
    )
    assert evaluate_non_delivery(state, now_mono=2.0, publication_id="o1", **kwargs).reanchor is False
    assert evaluate_non_delivery(state, now_mono=3.0, publication_id="o2", **kwargs).reanchor is False
    result = evaluate_non_delivery(state, now_mono=4.0, publication_id="o3", **kwargs)
    assert result.reanchor
    assert result.measured_w == -400


def test_fallback_timeout_needs_a_later_publication() -> None:
    state = NonDeliveryState()
    kwargs = dict(
        accepted_command_w=800,
        measured_power_w=0.0,
        measured_fresh=True,
        last_write_mono=0.0,
        actuator_latency_s=1.0,
        consecutive_required=10,
        fallback_timeout_s=15.0,
    )
    assert evaluate_non_delivery(state, now_mono=2.0, publication_id="f1", **kwargs).reanchor is False
    result = evaluate_non_delivery(state, now_mono=18.0, publication_id="f2", **kwargs)
    assert result.reanchor
    assert result.measured_w == 0


def test_reanchor_does_not_accept_measured_power() -> None:
    ledger = CommandLedger(accepted_w=1000)
    ledger.last_success_mono = 0.0
    pd_state = PDState()
    pd_state.requested_w = 1000
    pd_state.derivative_filtered = 25.0
    params = PDParams(kp=0.35, kd=1.0, battery_actuator_latency_s=1.0)
    result = None
    for index, stamp in enumerate((2.0, 3.0, 4.0), start=1):
        result = consider_battery_delivery(
            ledger,
            pd_state,
            params,
            measured_power_w=40.0,
            measured_fresh=True,
            now_mono=stamp,
            publication_id=f"m{index}",
            consecutive_required=3,
            fallback_timeout_s=60.0,
        )
    assert result is not None and result.reanchor
    assert ledger.accepted_w == 1000
    assert pd_state.requested_w == 40
    assert ledger.anchored_from_measurement
    assert pd_state.derivative_filtered == 0.0
    assert pd_state.last_direction == 1

    stepped = pd_step(
        pd_state,
        params,
        grid_raw_w=0.0,
        now_mono=5.0,
        source_mono=5.0,
        publication_id="after",
    )
    assert stepped.proposed_command_w == 40
    assert stepped.d_term == 0.0
    assert ledger.accepted_w == 1000


def test_commands_inside_the_noise_floor_are_not_non_delivery() -> None:
    kwargs = dict(
        measured_power_w=0.0,
        measured_fresh=True,
        last_write_mono=0.0,
        actuator_latency_s=1.0,
        consecutive_required=3,
        fallback_timeout_s=60.0,
    )
    for sign in (1, -1):
        for watts in range(1, 25):
            state = NonDeliveryState()
            command = sign * watts
            for index in range(4):
                result = evaluate_non_delivery(
                    state,
                    accepted_command_w=command,
                    now_mono=2.0 + index,
                    publication_id=f"{command}-{index}",
                    **kwargs,
                )
                assert result.reanchor is False
            assert state.shortfall_count == 0

    counted = NonDeliveryState()
    evaluate_non_delivery(
        counted,
        accepted_command_w=25,
        now_mono=2.0,
        publication_id="at-floor",
        **kwargs,
    )
    assert counted.shortfall_count == 1
    negative = NonDeliveryState()
    evaluate_non_delivery(
        negative,
        accepted_command_w=-25,
        now_mono=2.0,
        publication_id="at-floor-neg",
        **kwargs,
    )
    assert negative.shortfall_count == 1
