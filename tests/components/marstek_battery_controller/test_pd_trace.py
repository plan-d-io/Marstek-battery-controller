"""Trace: household load steps, cadence independence, and irregular publications."""

from __future__ import annotations

from custom_components.marstek_battery_controller.battery_transport import (
    deliver_command,
)
from custom_components.marstek_battery_controller.command_ledger import (
    CommandLedger,
    consider_battery_delivery,
)
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


class _Transport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[int] = []

    async def async_send_command(self, command_w: int) -> None:
        if self.fail:
            raise RuntimeError("modbus down")
        self.sent.append(command_w)

    async def async_release(self) -> None:
        return None


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
    """Load step, delayed battery, failed write, then a cancelled reversal."""
    params = PDParams(min_interval_s=0.5)
    state = PDState()
    tracker = PublicationTracker()
    ledger = CommandLedger()
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
        result = control_grid(state, params, tracker, now_mono=t)
        assert result.integrate_applied
        again = control_grid(state, params, tracker, now_mono=t + 0.05)
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
        decision = ledger.submit(command)
        assert decision.send_w == command
        assert ledger.succeed(t, state) == command

    assert publish_grid(0.0, 0.0, "g0") == 0

    first = publish_grid(900.0, 2.5, "g1")
    assert first > 0
    accept(first, 2.5)
    assert ledger.accepted_w == first
    assert state.last_direction == 1

    assisted = publish_grid(1400.0, 3.2, "g2")
    assert assisted >= first
    assert state.last_d_term > 0
    accept(assisted, 3.2)

    tracker.publish("battery", 0.0, source_mono=4.0, receive_mono=4.0, publication_id="b0")
    early = consider_battery_delivery(
        ledger,
        state,
        params,
        measured_power_w=tracker.battery.value,
        measured_fresh=tracker.battery_is_fresh(4.0),
        now_mono=4.0,
        publication_id="b0",
    )
    assert early.reanchor is False
    assert ledger.accepted_w == assisted

    reanchored = None
    for index, stamp in enumerate((7.0, 9.0, 11.0), start=1):
        tracker.publish(
            "battery",
            40.0,
            source_mono=stamp,
            receive_mono=stamp,
            publication_id=f"b{index}",
        )
        assert control_grid(state, params, tracker, now_mono=stamp).integrate_applied is False
        reanchored = consider_battery_delivery(
            ledger,
            state,
            params,
            measured_power_w=40.0,
            measured_fresh=True,
            now_mono=stamp,
            publication_id=f"b{index}",
        )
    assert reanchored is not None and reanchored.reanchor
    assert state.requested_w == 40
    assert ledger.accepted_w == assisted
    assert ledger.anchored_from_measurement
    assert state.last_proposed_w == 40

    failed = ledger.submit(40)
    assert failed.send_w == 40
    ledger.fail()
    assert ledger.accepted_w == assisted
    assert state.requested_w == 40

    recovered = ledger.submit(40)
    assert recovered.send_w == 40
    ledger.succeed(11.1, state)
    assert ledger.accepted_w == 40
    assert ledger.anchored_from_measurement is False

    previous_command = 40
    previous_t = 11.1
    held = publish_grid(-700.0, 12.0, "g3")
    assert held == 0
    assert state.last_direction == 1
    assert state.opposite_since_mono == 12.0
    accept(0, 12.0)
    assert state.last_direction == 1

    watchdog = control_grid(state, params, tracker, now_mono=13.0, opportunity=False)
    assert watchdog.integrate_applied is False
    assert watchdog.proposed_command_w == 0
    assert watchdog.p_term == 0.0
    assert watchdog.d_term == 0.0
    assert state.last_direction == 1
    assert state.opposite_since_mono == 12.0

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
    assert state.last_direction == 1

    late_watchdog = control_grid(
        state,
        params,
        tracker,
        now_mono=20.0 + hold_s + 5.0,
        opportunity=False,
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
    untouched = control_grid(state, params, tracker, now_mono=40.0)
    assert untouched.integrate_applied is False
    assert state.grid_filtered == filtered


async def _run_constant_load(
    times: list[float],
    min_interval: float,
    *,
    grid: float = 400.0,
    fail_at: float | None = None,
) -> tuple[int, int, int]:
    """Integrate a constant import and accept every new request.

    Returns ``(requested, accepted, request_after_first_opportunity)``.
    """
    params = PDParams(
        kp=0.35,
        kd=0.0,
        deadband_w=40.0,
        min_interval_s=min_interval,
        max_power_change_w=5000.0,
    )
    state = PDState()
    tracker = PublicationTracker()
    ledger = CommandLedger()
    reported = 0
    first: int | None = None
    for index, stamp in enumerate(times):
        tracker.publish(
            "grid",
            grid,
            source_mono=stamp,
            receive_mono=stamp,
            publication_id=f"c-{min_interval}-{index}-{stamp}",
        )
        result = control_grid(state, params, tracker, now_mono=stamp)
        if not result.integrate_applied:
            continue
        if first is None:
            first = state.requested_w
        if fail_at is not None and abs(stamp - fail_at) < 1e-9:
            failed = await deliver_command(
                _Transport(fail=True),
                ledger,
                state.requested_w,
                stamp,
                pd_state=state,
            )
            assert failed.accepted is False
            reported = ledger.accepted_w
            continue
        if state.requested_w != reported:
            decision = ledger.submit(state.requested_w)
            assert decision.send_w == state.requested_w
            ledger.succeed(stamp, state)
            reported = ledger.accepted_w
    assert first is not None
    return state.requested_w, ledger.accepted_w, first


def _times(period: float, horizon: float = 20.0) -> list[float]:
    count = int(round(horizon / period))
    return [round(index * period, 6) for index in range(count + 1)]


async def test_pd_convergence_is_comparable_across_cadences() -> None:
    """Same load, different publication and opportunity periods.

    After the first opportunity, accumulated P is ``kp * error * elapsed / 2``
    because each step uses the real source gap and the gaps sum to the horizon.
    The first opportunity has no measured gap and uses ``min_interval_s``, so
    absolute commands differ by that one step. Integer rounding is at most half
    a watt per step. A 6 s gap, from a 2 s meter behind a 5 s gate, is clipped
    by the open-loop cap ``1/kp`` (about 5.7 s) and can end one gate early.
    """
    horizon = 20.0
    growths: list[tuple[str, int]] = []
    for period in (0.5, 1.0, 2.0, 5.0):
        for minimum in (1.0, 2.0, 5.0):
            requested, accepted, first = await _run_constant_load(
                _times(period, horizon),
                minimum,
            )
            assert accepted == requested
            growths.append((f"pub {period}s / gate {minimum}s", requested - first))

    aligned = [
        growth
        for label, growth in growths
        if label != "pub 2.0s / gate 5.0s"
    ]
    # 20 steps of rounding, plus the EMA staying on a constant sample.
    assert max(aligned) - min(aligned) <= 40, growths
    # The misaligned cell integrates 18 s of capped 6 s gaps instead of 20 s.
    slow = dict(growths)["pub 2.0s / gate 5.0s"]
    assert abs(slow - aligned[0]) <= 250

    irregular = [0.0, 0.4, 1.1, 2.6, 5.0, 8.5, 13.0, 17.5, 20.0]
    dropped = [0.0, 0.4, 1.1, 5.0, 8.5, 13.0, 20.0]
    for minimum in (1.0, 2.0, 5.0):
        irregular_growth = (await _run_constant_load(irregular, minimum))[0]
        irregular_growth -= (await _run_constant_load(irregular, minimum))[2]
        dropped_requested, dropped_accepted, dropped_first = await _run_constant_load(
            dropped, minimum
        )
        assert dropped_accepted == dropped_requested
        assert abs((dropped_requested - dropped_first) - aligned[0]) <= 250
        assert abs(irregular_growth - aligned[0]) <= 250


async def test_interval_alone_does_not_write_and_failures_do_not_reset_the_request() -> None:
    params = PDParams(kp=0.35, kd=0.0, min_interval_s=1.0, max_power_change_w=5000.0)
    state = PDState()
    tracker = PublicationTracker()
    ledger = CommandLedger()
    transport = _Transport()
    tracker.publish("grid", 400.0, source_mono=0.0, receive_mono=0.0, publication_id="w0")
    first = control_grid(state, params, tracker, now_mono=0.0)
    assert first.integrate_applied
    assert (
        await deliver_command(transport, ledger, state.requested_w, 0.0, pd_state=state)
    ).accepted
    accepted = ledger.accepted_w
    sent = list(transport.sent)

    frozen = control_grid(state, params, tracker, now_mono=2.0, opportunity=False)
    assert frozen.integrate_applied is False
    assert frozen.p_term == 0.0
    assert frozen.d_term == 0.0
    idle = control_grid(state, params, tracker, now_mono=4.0)
    assert idle.integrate_applied is False
    assert transport.sent == sent
    assert ledger.accepted_w == accepted
    assert state.requested_w == accepted

    requested_before = state.requested_w
    derivative = state.derivative_filtered
    watchdog = control_grid(state, params, tracker, now_mono=4.5, opportunity=False)
    assert watchdog.p_term == 0.0
    assert watchdog.d_term == 0.0
    assert state.derivative_filtered == derivative
    assert state.requested_w == requested_before

    failing = _Transport(fail=True)
    fail_ledger = CommandLedger()
    fail_state = PDState()
    fail_tracker = PublicationTracker()
    fail_tracker.publish("grid", 400.0, source_mono=0.0, receive_mono=0.0, publication_id="f0")
    control_grid(fail_state, params, fail_tracker, now_mono=0.0)
    built = fail_state.requested_w
    assert (
        await deliver_command(failing, fail_ledger, built, 0.0, pd_state=fail_state)
    ).accepted is False
    assert fail_ledger.accepted_w == 0
    assert fail_state.requested_w == built
    fail_tracker.publish("grid", 400.0, source_mono=1.0, receive_mono=1.0, publication_id="f1")
    control_grid(fail_state, params, fail_tracker, now_mono=1.0)
    assert fail_state.requested_w > built
    assert fail_ledger.accepted_w == 0
    assert (
        await deliver_command(
            _Transport(), fail_ledger, fail_state.requested_w, 1.0, pd_state=fail_state
        )
    ).accepted
    recovered = fail_state.requested_w
    assert fail_ledger.accepted_w == recovered
    fail_tracker.publish("grid", 400.0, source_mono=2.0, receive_mono=2.0, publication_id="f2")
    control_grid(fail_state, params, fail_tracker, now_mono=2.0)
    assert fail_state.requested_w > recovered
