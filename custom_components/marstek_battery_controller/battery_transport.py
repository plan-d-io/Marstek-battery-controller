"""Battery write contract. The concrete marstek_modbus adapter is not wired here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .command_ledger import CommandLedger


@runtime_checkable
class BatteryTransport(Protocol):
    """Signed battery transport. Positive watts discharge; negative watts charge."""

    async def async_send_command(self, command_w: int) -> None:
        """Write one command. Raising means the command was not accepted."""

    async def async_release(self) -> None:
        """Release firmware control. This does not by itself accept a command."""


@dataclass(frozen=True)
class DeliveryResult:
    """Result of attempting one ledger submission."""

    accepted: bool
    sent_w: int | None
    queued: bool


async def deliver_command(
    transport: BatteryTransport,
    ledger: CommandLedger,
    command_w: int,
    now_mono: float,
) -> DeliveryResult:
    """Send when the ledger allows it. Only a successful write is accepted."""
    decision = ledger.submit(command_w, now_mono)
    if decision.send_w is None:
        return DeliveryResult(accepted=False, sent_w=None, queued=decision.queued)
    try:
        await transport.async_send_command(decision.send_w)
    except Exception:
        ledger.fail()
        return DeliveryResult(
            accepted=False,
            sent_w=None,
            queued=ledger.pending_w is not None,
        )
    accepted = ledger.succeed(now_mono)
    return DeliveryResult(
        accepted=True,
        sent_w=accepted,
        queued=ledger.pending_w is not None,
    )
