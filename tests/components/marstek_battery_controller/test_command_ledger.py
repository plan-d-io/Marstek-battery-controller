"""Accepted-command ledger, write queue, and transport contract."""

from __future__ import annotations

import asyncio

from custom_components.marstek_battery_controller.battery_transport import (
    deliver_command,
)
from custom_components.marstek_battery_controller.command_ledger import (
    CommandLedger,
    should_skip_write,
)
from custom_components.marstek_battery_controller.const import DEFAULT_SEND_INTERVAL


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


def test_skip_threshold_still_writes_zero_and_reversals() -> None:
    assert should_skip_write(100, 100, write_threshold_w=10)
    assert should_skip_write(100, 105, write_threshold_w=10)
    assert should_skip_write(100, 120, write_threshold_w=10) is False
    assert should_skip_write(100, 0, write_threshold_w=10) is False
    assert should_skip_write(100, -50, write_threshold_w=10) is False


def test_default_write_interval_matches_production_send_interval() -> None:
    assert CommandLedger().min_interval_s == DEFAULT_SEND_INTERVAL == 5.0


def test_failed_write_does_not_advance_accepted() -> None:
    ledger = CommandLedger(accepted_w=100, reference_w=100)
    transport = _Transport(fail=True)
    result = asyncio.run(deliver_command(transport, ledger, 400, 1.0))
    assert result.accepted is False
    assert result.sent_w is None
    assert ledger.accepted_w == 100
    assert ledger.reference_w == 100
    assert ledger.in_flight_w is None
    assert transport.sent == []
    asyncio.run(transport.async_release())
    assert ledger.accepted_w == 100
    assert transport.sent == ["release"]


def test_newest_command_replaces_the_queue() -> None:
    ledger = CommandLedger()
    ledger.min_interval_s = 5.0
    first = ledger.submit(100, 0.0)
    assert first.send_w == 100
    assert ledger.submit(200, 0.0).send_w is None
    assert ledger.submit(350, 0.1).send_w is None
    assert ledger.pending_w == 350
    assert ledger.accepted_w == 0

    sent = ledger.succeed(0.2)
    assert sent == 100
    assert ledger.accepted_w == 100
    assert ledger.pending_w == 350

    too_soon = ledger.poll(1.0)
    assert too_soon.send_w is None
    assert ledger.pending_w == 350
    ready = ledger.poll(5.2)
    assert ready.send_w == 350
    assert ledger.succeed(5.2) == 350
    assert ledger.accepted_w == 350
    assert ledger.pending_w is None


def test_zero_bypasses_the_write_interval() -> None:
    ledger = CommandLedger(min_interval_s=5.0)
    assert ledger.submit(500, 0.0).send_w == 500
    ledger.succeed(0.0)
    stopped = ledger.submit(0, 0.2)
    assert stopped.send_w == 0
    assert ledger.succeed(0.2) == 0
    assert ledger.accepted_w == 0


def test_successful_write_clears_a_measurement_anchor() -> None:
    ledger = CommandLedger(accepted_w=1000, reference_w=80, anchored_from_measurement=True)
    assert ledger.submit(80, 0.0).send_w == 80
    ledger.succeed(0.0)
    assert ledger.accepted_w == 80
    assert ledger.reference_w == 80
    assert ledger.anchored_from_measurement is False
