"""Accepted-command ledger, write suppression, and transport contract."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.marstek_battery_controller.battery_transport import (
    deliver_command,
)
from custom_components.marstek_battery_controller.command_ledger import (
    CommandLedger,
    consider_battery_delivery,
    decide_actuator_write,
)
from custom_components.marstek_battery_controller.const import DEFAULT_SEND_INTERVAL
from custom_components.marstek_battery_controller.pd_controller import (
    DEFAULT_MIN_INTERVAL_S,
    PDParams,
    PDState,
    pd_step,
    reanchor_pd_state,
)


class _Transport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[int | str] = []

    async def async_send_command(self, command_w: int) -> None:
        if self.fail:
            raise RuntimeError("modbus down")
        self.sent.append(command_w)

    async def async_release(self) -> None:
        self.sent.append("release")


class _HangingTransport:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.hold = asyncio.Event()

    async def async_send_command(self, command_w: int) -> None:
        self.started.set()
        await self.hold.wait()

    async def async_release(self) -> None:
        return None


def _decision(**overrides: object):
    values = dict(
        requested_w=400,
        reported_command_w=400,
        measured_power_w=200.0,
        measured_fresh=True,
        now_mono=10.0,
        last_change_mono=0.0,
        actuator_latency_s=3.0,
    )
    values.update(overrides)
    return decide_actuator_write(**values)  # type: ignore[arg-type]


def test_write_when_request_differs_from_reported_command() -> None:
    assert _decision(requested_w=400, reported_command_w=0).action == "write"
    assert _decision(requested_w=0, reported_command_w=400).action == "write"
    assert _decision(requested_w=0, reported_command_w=400).bypass_interval
    # A 50 W difference is still a real command change. There is no watt threshold.
    assert _decision(requested_w=300, reported_command_w=250).action == "write"


def test_skip_when_delivery_matches_and_reassert_when_it_does_not() -> None:
    assert _decision(measured_power_w=200.0).action == "skip"
    # 10% of 400 W is 40 W. 10 W is the same command but not delivering.
    assert _decision(measured_power_w=10.0).action == "reassert"
    # Inside the actuator delay this is not yet non-delivery.
    waiting = _decision(measured_power_w=0.0, now_mono=1.0, last_change_mono=0.0)
    assert waiting.action == "wait"
    for _ in range(5):
        assert _decision().action == "skip"


def test_zero_is_not_repeated_while_idle_and_is_reasserted_on_escape() -> None:
    idle = _decision(requested_w=0, reported_command_w=0, measured_power_w=5.0)
    assert idle.action == "skip"
    escaped = _decision(requested_w=0, reported_command_w=0, measured_power_w=80.0)
    assert escaped.action == "reassert"
    assert escaped.bypass_interval
    settling = _decision(
        requested_w=0,
        reported_command_w=0,
        measured_power_w=80.0,
        now_mono=1.0,
        last_change_mono=0.0,
    )
    assert settling.action == "wait"


def test_delivery_floor_follows_the_ten_percent_rule() -> None:
    # 1000 W needs about 100 W. 40 W does not qualify.
    assert _decision(requested_w=1000, reported_command_w=1000, measured_power_w=40).action == "reassert"
    assert _decision(requested_w=1000, reported_command_w=1000, measured_power_w=100).action == "skip"
    # At 100 W the Omnibattery floor is max(25 W, 10 W).
    assert _decision(requested_w=100, reported_command_w=100, measured_power_w=25).action == "skip"
    assert _decision(requested_w=100, reported_command_w=100, measured_power_w=10).action == "reassert"


def test_pure_interval_default_is_not_the_entity_default() -> None:
    assert DEFAULT_MIN_INTERVAL_S == 1.0
    assert DEFAULT_SEND_INTERVAL == 5.0
    assert not hasattr(CommandLedger(), "min_interval_s")


def test_failed_write_does_not_advance_accepted() -> None:
    ledger = CommandLedger(accepted_w=100)
    state = PDState()
    state.requested_w = 400
    transport = _Transport(fail=True)
    result = asyncio.run(
        deliver_command(transport, ledger, 400, 1.0, pd_state=state)
    )
    assert result.accepted is False
    assert result.sent_w is None
    assert ledger.accepted_w == 100
    assert state.requested_w == 400
    assert state.last_direction == 0
    assert ledger.in_flight_w is None
    assert transport.sent == []
    asyncio.run(transport.async_release())
    assert ledger.accepted_w == 100
    assert transport.sent == ["release"]


def test_unaccepted_discharge_does_not_arm_a_reversal_hold() -> None:
    state = PDState()
    params = PDParams(kp=0.35, kd=0.0, max_power_change_w=5000.0)
    proposed = pd_step(
        state,
        params,
        grid_raw_w=2000.0,
        now_mono=0.0,
        source_mono=0.0,
        publication_id="up",
    )
    assert proposed.proposed_command_w == 350
    ledger = CommandLedger(accepted_w=0)
    failed = asyncio.run(
        deliver_command(_Transport(fail=True), ledger, 350, 0.0, pd_state=state)
    )
    assert failed.accepted is False
    assert ledger.accepted_w == 0
    assert state.last_direction == 0
    charging = pd_step(
        state,
        params,
        grid_raw_w=-2000.0,
        now_mono=2.0,
        source_mono=2.0,
        publication_id="down",
    )
    assert charging.zero_cross_hold is False
    assert charging.proposed_command_w < 0

    held_state = PDState()
    pd_step(
        held_state,
        params,
        grid_raw_w=2000.0,
        now_mono=0.0,
        source_mono=0.0,
        publication_id="up2",
    )
    accepted = asyncio.run(
        deliver_command(_Transport(), ledger_ok := CommandLedger(), 350, 0.0, pd_state=held_state)
    )
    assert accepted.accepted
    assert held_state.last_direction == 1
    held = pd_step(
        held_state,
        params,
        grid_raw_w=-2000.0,
        now_mono=2.0,
        source_mono=2.0,
        publication_id="down2",
    )
    assert held.zero_cross_hold
    assert held.proposed_command_w == 0
    assert held_state.last_direction == 1


def test_newest_command_waits_for_an_explicit_submit() -> None:
    ledger = CommandLedger()
    first = ledger.submit(100)
    assert first.send_w == 100
    assert ledger.submit(200).send_w is None
    assert ledger.submit(350).send_w is None
    assert ledger.pending_w == 350
    assert ledger.accepted_w == 0

    sent = ledger.succeed(0.2)
    assert sent == 100
    assert ledger.accepted_w == 100
    assert ledger.pending_w == 350
    assert ledger.in_flight_w is None
    # Time passing does not flush the queue.
    assert ledger.pending_w == 350
    ready = ledger.submit(350)
    assert ready.send_w == 350
    assert ledger.succeed(5.2) == 350
    assert ledger.accepted_w == 350
    assert ledger.pending_w is None


def test_zero_can_be_sent_immediately_after_another_command() -> None:
    ledger = CommandLedger()
    assert ledger.submit(500).send_w == 500
    ledger.succeed(0.0)
    stopped = ledger.submit(0, safety=True)
    assert stopped.send_w == 0
    assert ledger.succeed(0.2) == 0
    assert ledger.accepted_w == 0


def test_successful_write_of_the_anchored_request_clears_the_flag() -> None:
    ledger = CommandLedger(accepted_w=1000)
    state = PDState()
    reanchor_pd_state(state, 80)
    ledger.reanchor_to_measurement()
    assert ledger.submit(80).send_w == 80
    ledger.succeed(0.0, state)
    assert ledger.accepted_w == 80
    assert state.requested_w == 80
    assert ledger.anchored_from_measurement is False


def test_reanchor_discards_pending_and_leaves_the_in_flight_write() -> None:
    def fire(ledger: CommandLedger, state: PDState):
        ledger.last_success_mono = 0.0
        ledger.non_delivery.shortfall_count = 2
        ledger.non_delivery.first_shortfall_mono = 1.0
        ledger.non_delivery.last_checked_command_w = ledger.accepted_w
        return consider_battery_delivery(
            ledger,
            state,
            PDParams(battery_actuator_latency_s=1.0),
            measured_power_w=40.0,
            measured_fresh=True,
            now_mono=10.0,
            publication_id="reanchor",
            consecutive_required=3,
            fallback_timeout_s=60.0,
        )

    pending_only = CommandLedger(accepted_w=1000)
    pending_state = PDState()
    pending_state.requested_w = 1000
    assert pending_only.submit(500).send_w == 500
    assert pending_only.submit(800).queued
    pending_only.succeed(0.5)
    assert pending_only.pending_w == 800
    assert fire(pending_only, pending_state).reanchor
    assert pending_only.pending_w is None
    assert pending_only.accepted_w == 500
    assert pending_only.in_flight_w is None
    assert pending_state.requested_w == 40
    assert pending_state.requested_w != pending_only.accepted_w

    inflight = CommandLedger(accepted_w=1000)
    inflight_state = PDState()
    inflight_state.requested_w = 1000
    assert inflight.submit(700).send_w == 700
    assert inflight.submit(900).queued
    assert fire(inflight, inflight_state).reanchor
    assert inflight.in_flight_w == 700
    assert inflight.pending_w is None
    assert inflight.accepted_w == 1000
    assert inflight_state.requested_w == 40

    assert inflight.succeed(11.0, inflight_state) == 700
    assert inflight.accepted_w == 700
    assert inflight.pending_w is None
    assert inflight_state.requested_w == 40

    failed = CommandLedger(accepted_w=1000)
    failed_state = PDState()
    failed_state.requested_w = 1000
    assert failed.submit(700).send_w == 700
    assert failed.submit(900).queued
    assert fire(failed, failed_state).reanchor
    failed.fail()
    assert failed.accepted_w == 1000
    assert failed.in_flight_w is None
    assert failed.pending_w is None
    assert failed_state.requested_w == 40


def test_cancelled_delivery_does_not_deadlock_and_can_be_reconciled() -> None:
    ledger = CommandLedger(accepted_w=100)
    state = PDState()
    state.requested_w = 350
    transport = _HangingTransport()

    async def scenario() -> None:
        task = asyncio.create_task(
            deliver_command(transport, ledger, 350, 0.0, pd_state=state)
        )
        await transport.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert ledger.accepted_w == 100
        assert ledger.in_flight_w is None
        assert ledger.delivery_uncertain
        assert ledger.uncertain_command_w == 350
        assert state.requested_w == 350
        assert state.last_direction == 0

        recovered = await deliver_command(
            _Transport(),
            ledger,
            0,
            1.0,
            pd_state=state,
            safety=True,
        )
        assert recovered.accepted
        assert ledger.accepted_w == 0
        assert ledger.delivery_uncertain is False
        assert ledger.uncertain_command_w is None
        assert ledger.in_flight_w is None

    asyncio.run(scenario())


def test_reported_readback_clears_uncertainty() -> None:
    ledger = CommandLedger(accepted_w=100)
    state = PDState()
    ledger.delivery_uncertain = True
    ledger.uncertain_command_w = 350
    ledger.reconcile_reported(350, state)
    assert ledger.accepted_w == 350
    assert ledger.delivery_uncertain is False
    assert ledger.uncertain_command_w is None
    assert state.last_direction == 1
    assert ledger.in_flight_w is None


def test_readback_during_an_awaited_write_does_not_drop_the_in_flight_slot() -> None:
    ledger = CommandLedger(accepted_w=100)
    state = PDState()
    transport = _HangingTransport()

    async def scenario() -> None:
        task = asyncio.create_task(
            deliver_command(transport, ledger, 350, 4.0, pd_state=state)
        )
        await transport.started.wait()
        assert ledger.in_flight_w == 350
        ledger.reconcile_reported(80, state)
        assert ledger.in_flight_w == 350
        assert ledger.accepted_w == 100
        assert state.last_direction == 0
        transport.hold.set()
        result = await task
        assert result.accepted
        assert result.sent_w == 350
        assert ledger.accepted_w == 350
        assert ledger.in_flight_w is None
        assert ledger.last_success_mono == 4.0
        assert state.last_direction == 1

    asyncio.run(scenario())


def test_settling_starts_when_the_transport_call_completes() -> None:
    ledger = CommandLedger()
    state = PDState()
    mono = {"now": 10.0}

    class _SlowTransport:
        async def async_send_command(self, command_w: int) -> None:
            mono["now"] = 15.0

        async def async_release(self) -> None:
            return None

    result = asyncio.run(
        deliver_command(
            _SlowTransport(),
            ledger,
            400,
            10.0,
            pd_state=state,
            clock=lambda: mono["now"],
        )
    )
    assert result.accepted
    assert ledger.last_success_mono == 15.0
    waiting = decide_actuator_write(
        requested_w=400,
        reported_command_w=400,
        measured_power_w=0.0,
        measured_fresh=True,
        now_mono=17.0,
        last_change_mono=ledger.last_success_mono,
        actuator_latency_s=3.0,
    )
    assert waiting.action == "wait"
    consumed = decide_actuator_write(
        requested_w=400,
        reported_command_w=400,
        measured_power_w=0.0,
        measured_fresh=True,
        now_mono=17.0,
        last_change_mono=10.0,
        actuator_latency_s=3.0,
    )
    assert consumed.action == "reassert"
