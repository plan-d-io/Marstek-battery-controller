"""Trace: household load steps, battery delay, and irregular publications."""

from __future__ import annotations

from custom_components.marstek_battery_controller.command_ledger import (
    CommandLedger,
    consider_battery_delivery,
)
from custom_components.marstek_battery_controller.non_delivery import NonDeliveryState
from custom_components.marstek_battery_controller.pd_controller import (
    NOMINAL_CYCLE_S,
    PDParams,
    PDState,
    control_grid,
    zero_cross_hold_s,
)
from custom_components.marstek_battery_controller.publications import PublicationTracker


def _max_ramp(params: PDParams, dt: float) -> float:
    bounded = min(max(dt, 0.1), 10.0)
    return params.max_power_change_w * (bounded / NOMINAL_CYCLE_S) + 1.0


def test_modules_stay_free_of_home_assistant_and_ev_control() -> None:
    import ast
    from pathlib import Path

    root = Path("custom_components/marstek_battery_controller")
    names = (
        "pd_controller.py",
        "publications.py",
        "non_delivery.py",
        "command_ledger.py",
        "battery_transport.py",
    )
    for name in names:
        text = (root / name).read_text(encoding="utf-8")
        tree = ast.parse(text)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        for module in imported:
            assert "homeassistant" not in module
            assert "ev_" not in module
        lowered = text.lower()
        assert "charger" not in lowered
        assert "electric vehicle" not in lowered


def test_household_trace_with_delay_and_irregular_grid() -> None:
    """Load step, delayed battery, failed write, then a cancelled reversal.

    Write pacing is covered in the ledger tests. This trace accepts commands
    immediately so the publication times are the only clock.
    """
    params = PDParams()
    state = PDState()
    tracker = PublicationTracker()
    ledger = CommandLedger()
    ledger.min_interval_s = 0.0
    non_delivery = NonDeliveryState()
    hold_s = zero_cross_hold_s(params)
    previous_command = 0
    previous_t: float | None = None

    def publish_grid(value: float, t: float, publication_id: str) -> int:
        nonlocal previous_command, previous_t
        tracker.publish(
            "grid",
            value,
            source_mono=t,
            receive_mono=t,
            publication_id=publication_id,
        )
        result = control_grid(
            state,
            params,
            tracker,
            now_mono=t,
            reference_command_w=ledger.reference_w,
        )
        assert result.integrate_applied
        again = control_grid(
            state,
            params,
            tracker,
            now_mono=t + 0.05,
            reference_command_w=ledger.reference_w,
        )
        assert again.integrate_applied is False
        assert again.proposed_command_w == result.proposed_command_w
        assert abs(result.proposed_command_w) <= params.max_battery_power_w
        if previous_t is not None and previous_command != 0 and result.proposed_command_w != 0:
            same_direction = (previous_command > 0) == (result.proposed_command_w > 0)
            assert same_direction, (previous_command, result.proposed_command_w)
            assert abs(result.proposed_command_w - previous_command) <= _max_ramp(
                params, t - previous_t
            )
        previous_command = result.proposed_command_w
        previous_t = t
        return result.proposed_command_w

    def accept(command: int, t: float) -> None:
        decision = ledger.submit(command, t)
        assert decision.send_w == command
        assert ledger.succeed(t) == command

    # Quiet house. A repeated read must not move the controller.
    assert publish_grid(0.0, 0.0, "g0") == 0

    # Kettle. First correction discharges, and the derivative is still seeding.
    first = publish_grid(900.0, 2.5, "g1")
    assert first > 0
    accept(first, 2.5)
    assert ledger.accepted_w == first

    # Second load a short, irregular interval later. Derivative must assist.
    assisted = publish_grid(1400.0, 3.2, "g2")
    assert assisted >= first
    assert state.last_d_term > 0
    accept(assisted, 3.2)

    # Battery has not moved yet. Inside the actuator delay this is not failure.
    tracker.publish("battery", 0.0, source_mono=4.0, receive_mono=4.0, publication_id="b0")
    early = consider_battery_delivery(
        ledger,
        state,
        non_delivery,
        measured_power_w=tracker.battery.value,
        measured_fresh=tracker.battery_is_fresh(4.0),
        now_mono=4.0,
        actuator_latency_s=params.battery_actuator_latency_s,
        publication_id="b0",
    )
    assert early.reanchor is False
    assert ledger.accepted_w == assisted

    # Three fresh battery publications after the delay, still near zero.
    reanchored = None
    for index, stamp in enumerate((7.0, 9.0, 11.0), start=1):
        tracker.publish(
            "battery",
            40.0,
            source_mono=stamp,
            receive_mono=stamp,
            publication_id=f"b{index}",
        )
        # Battery telemetry must not look like a new grid publication.
        assert control_grid(
            state,
            params,
            tracker,
            now_mono=stamp,
            reference_command_w=ledger.reference_w,
        ).integrate_applied is False
        reanchored = consider_battery_delivery(
            ledger,
            state,
            non_delivery,
            measured_power_w=40.0,
            measured_fresh=True,
            now_mono=stamp,
            actuator_latency_s=params.battery_actuator_latency_s,
            publication_id=f"b{index}",
        )
    assert reanchored is not None and reanchored.reanchor
    assert ledger.reference_w == 40
    assert ledger.accepted_w == assisted
    assert ledger.anchored_from_measurement
    assert state.last_proposed_w == 40

    failed = ledger.submit(40, 11.0)
    assert failed.send_w == 40
    ledger.fail()
    assert ledger.accepted_w == assisted
    assert ledger.reference_w == 40

    recovered = ledger.submit(40, 11.1)
    assert recovered.send_w == 40
    ledger.succeed(11.1)
    assert ledger.accepted_w == 40
    assert ledger.anchored_from_measurement is False

    # Export while the last direction is discharge: stop, do not charge yet.
    previous_command = 40
    previous_t = 11.1
    held = publish_grid(-700.0, 12.0, "g3")
    assert held == 0
    assert state.last_direction == 1
    assert state.opposite_since_mono == 12.0
    accept(0, 12.0)

    watchdog = control_grid(
        state,
        params,
        tracker,
        now_mono=13.0,
        reference_command_w=ledger.reference_w,
    )
    assert watchdog.integrate_applied is False
    assert watchdog.proposed_command_w == 0
    assert state.last_direction == 1
    assert state.opposite_since_mono == 12.0

    # The export disappears before the hold ends. No later stale reversal.
    previous_command = 0
    previous_t = 12.0
    restored = publish_grid(600.0, 14.0, "g4")
    assert restored >= 0
    assert state.opposite_since_mono is None
    accept(restored, 14.0)

    previous_command = restored
    previous_t = 14.0
    armed = publish_grid(-800.0, 20.0, "g5")
    assert armed == 0
    assert state.opposite_since_mono == 20.0
    assert state.last_direction == 1
    accept(0, 20.0)

    # A watchdog past the deadline must not finish the hold or move its start.
    late_watchdog = control_grid(
        state,
        params,
        tracker,
        now_mono=20.0 + hold_s + 5.0,
        reference_command_w=0,
    )
    assert late_watchdog.proposed_command_w == 0
    assert late_watchdog.zero_cross_hold
    assert state.opposite_since_mono == 20.0
    assert state.last_direction == 1

    previous_command = 0
    previous_t = 20.0
    charging = publish_grid(-800.0, 20.0 + hold_s + 5.1, "g6")
    assert charging < 0
    assert abs(charging) <= params.max_power_change_w * (10.0 / NOMINAL_CYCLE_S) + 1

    filtered = state.grid_filtered
    stale = tracker.publish(
        "grid",
        5000.0,
        source_mono=10.0,
        receive_mono=40.0,
        publication_id="stale-old",
    )
    assert stale.recorded is False
    assert stale.new_feedback is False
    untouched = control_grid(
        state,
        params,
        tracker,
        now_mono=40.0,
        reference_command_w=ledger.reference_w,
    )
    assert untouched.integrate_applied is False
    assert state.grid_filtered == filtered
