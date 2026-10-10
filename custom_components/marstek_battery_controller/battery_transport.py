"""Transport contract for one serialized battery setpoint.

Positive watts discharge (grid import). Negative watts charge (grid export).
Zero releases forced power. This module does not talk to Home Assistant.
``asyncio.CancelledError`` is a ``BaseException``, so it is handled on its
own: the in-flight slot is released, the command is not accepted, and the
cancellation is re-raised.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .command_ledger import CommandLedger
from .pd_controller import PDState


class BatteryTransport(Protocol):
    """The eventual marstek_modbus adapter implements this."""

    async def async_send_command(self, command_w: int) -> None:
        """Apply a signed setpoint. Positive discharges, negative charges."""

    async def async_release(self) -> None:
        """Return the battery to its own control."""


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one attempt. A queued command was not sent on this call."""

    accepted: bool
    sent_w: int | None
    queued: bool
    uncertain: bool = False


async def deliver_command(
    transport: BatteryTransport,
    ledger: CommandLedger,
    command_w: int,
    now_mono: float,
    *,
    pd_state: PDState | None = None,
    safety: bool = False,
    reassert: bool = False,
    clock: Callable[[], float] | None = None,
) -> DeliveryResult:
    """Send one command, or queue it behind the write already in flight.

    Cancellation after the external call has started leaves delivery uncertain
    and does not mark the command accepted. The caller can then send an
    explicit safety zero or adopt a read-back through ``reconcile_reported``.

    Settling starts when the transport call returns, not when it was issued.
    Pass ``clock`` to sample monotonic time after the await. ``now_mono`` is
    the completion time only when no clock is injected, which matches an
    instantaneous test double.
    """
    decision = ledger.submit(command_w, safety=safety, reassert=reassert)
    if decision.send_w is None:
        return DeliveryResult(
            accepted=False,
            sent_w=None,
            queued=decision.queued,
            uncertain=ledger.delivery_uncertain,
        )

    call_started = False
    try:
        call_started = True
        await transport.async_send_command(decision.send_w)
    except asyncio.CancelledError:
        ledger.note_cancelled(decision.send_w, call_started=call_started)
        raise
    except Exception:
        ledger.fail()
        return DeliveryResult(
            accepted=False,
            sent_w=None,
            queued=ledger.pending_w is not None,
            uncertain=ledger.delivery_uncertain,
        )

    completed_mono = float(clock()) if clock is not None else float(now_mono)
    accepted = ledger.succeed(completed_mono, pd_state)
    return DeliveryResult(
        accepted=True,
        sent_w=accepted,
        queued=ledger.pending_w is not None,
        uncertain=False,
    )
