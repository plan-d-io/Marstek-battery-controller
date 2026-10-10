"""Accepted battery commands, write serialization, and measurement re-anchor.

Positive watts discharge and negative watts charge, matching ``modbus_writer``.
A proposal becomes accepted only after the transport call returns successfully.
While a write is in flight, a newer proposal replaces the pending one.
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import DEFAULT_SEND_INTERVAL
from .non_delivery import NonDeliveryResult, NonDeliveryState, evaluate_non_delivery
from .pd_controller import PDState, reanchor_pd_state


def should_skip_write(
    accepted_w: int,
    proposed_w: int,
    *,
    write_threshold_w: float,
) -> bool:
    """Skip a write that does not meaningfully change the accepted command."""
    if proposed_w == accepted_w:
        return True
    if write_threshold_w <= 0:
        return False
    if proposed_w == 0 or accepted_w == 0:
        return False
    if (proposed_w > 0) != (accepted_w > 0):
        return False
    return abs(proposed_w - accepted_w) < write_threshold_w


@dataclass(frozen=True)
class SubmitResult:
    """What the caller should hand to the transport right now."""

    send_w: int | None
    queued: bool
    skipped: bool


@dataclass
class CommandLedger:
    """Separates accepted commands, the PD reference, and the write queue.

    ``reference_w`` follows ``accepted_w`` until a measurement re-anchor.
    Re-anchoring never assigns the measured power to ``accepted_w``.
    """

    accepted_w: int = 0
    reference_w: int = 0
    anchored_from_measurement: bool = False
    in_flight_w: int | None = None
    pending_w: int | None = None
    last_success_mono: float | None = None
    # Matches the production send interval. Not a new user-facing control.
    min_interval_s: float = DEFAULT_SEND_INTERVAL
    write_threshold_w: float = 0.0

    def reanchor_to_measurement(self, measured_w: int) -> None:
        """Move the PD baseline to a measurement without accepting it."""
        self.reference_w = int(measured_w)
        self.anchored_from_measurement = True

    def submit(self, proposed_w: int, now_mono: float) -> SubmitResult:
        """Serialize writes. The newest proposal replaces any queued command."""
        if self.in_flight_w is not None:
            self.pending_w = int(proposed_w)
            return SubmitResult(send_w=None, queued=True, skipped=False)

        if should_skip_write(
            self.accepted_w,
            int(proposed_w),
            write_threshold_w=self.write_threshold_w,
        ):
            self.pending_w = None
            return SubmitResult(send_w=None, queued=False, skipped=True)

        if (
            proposed_w != 0
            and self.last_success_mono is not None
            and (now_mono - self.last_success_mono) < self.min_interval_s
        ):
            self.pending_w = int(proposed_w)
            return SubmitResult(send_w=None, queued=True, skipped=False)

        self.pending_w = None
        self.in_flight_w = int(proposed_w)
        return SubmitResult(send_w=self.in_flight_w, queued=False, skipped=False)

    def succeed(self, now_mono: float) -> int:
        """Accept the in-flight command. Pending work stays queued."""
        if self.in_flight_w is None:
            raise RuntimeError("no in-flight battery command to accept")
        sent = self.in_flight_w
        self.accepted_w = sent
        self.reference_w = sent
        self.anchored_from_measurement = False
        self.in_flight_w = None
        self.last_success_mono = now_mono
        if self.pending_w == sent:
            self.pending_w = None
        return sent

    def fail(self) -> None:
        """Drop the in-flight attempt. Accepted and reference state stay put."""
        if self.in_flight_w is None:
            raise RuntimeError("no in-flight battery command to fail")
        self.in_flight_w = None

    def poll(self, now_mono: float) -> SubmitResult:
        """Release the newest queued command once the write interval allows it."""
        if self.in_flight_w is not None or self.pending_w is None:
            return SubmitResult(
                send_w=None,
                queued=self.pending_w is not None,
                skipped=False,
            )
        nxt = self.pending_w
        self.pending_w = None
        return self.submit(nxt, now_mono)


def apply_measurement_reanchor(
    ledger: CommandLedger,
    pd_state: PDState,
    measured_w: int,
) -> None:
    """Re-anchor PD memory and the reference without accepting ``measured_w``."""
    ledger.reanchor_to_measurement(measured_w)
    reanchor_pd_state(pd_state, float(measured_w))


def consider_battery_delivery(
    ledger: CommandLedger,
    pd_state: PDState,
    non_delivery: NonDeliveryState,
    *,
    measured_power_w: float | None,
    measured_fresh: bool,
    now_mono: float,
    actuator_latency_s: float,
    publication_id: str | None = None,
    shortfall_threshold_w: float = 150.0,
    consecutive_required: int = 3,
    fallback_timeout_s: float = 15.0,
) -> NonDeliveryResult:
    """Re-anchor after sustained divergence. Never copies measurement into accepted.

    An in-flight write is allowed to finish first so a late success and a
    re-anchor cannot update the reference in an undefined order.
    """
    if ledger.in_flight_w is not None:
        return NonDeliveryResult(reanchor=False, measured_w=None, shortfall_w=0.0)

    result = evaluate_non_delivery(
        non_delivery,
        accepted_command_w=ledger.accepted_w,
        measured_power_w=measured_power_w,
        measured_fresh=measured_fresh,
        now_mono=now_mono,
        last_write_mono=ledger.last_success_mono,
        actuator_latency_s=actuator_latency_s,
        shortfall_threshold_w=shortfall_threshold_w,
        consecutive_required=consecutive_required,
        fallback_timeout_s=fallback_timeout_s,
        publication_id=publication_id,
    )
    if result.reanchor and result.measured_w is not None:
        apply_measurement_reanchor(ledger, pd_state, result.measured_w)
    return result
