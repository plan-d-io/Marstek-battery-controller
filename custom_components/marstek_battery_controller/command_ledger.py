"""Physical command ledger, separate from the PD request.

The controller's internal request is not a successful write. ``accepted_w``
changes only when a transport call returns. Measured power never becomes the
accepted command. A re-anchor discards a pending command calculated from the
old reference and leaves an in-flight write alone.

Ordinary writes are serialized. Time passing does not flush a pending command.
The minimum interval between control opportunities lives on ``PDParams`` and
defaults to 1 s. It is not a periodic heartbeat, and this ledger does not
embed the Home Assistant send-interval default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from .non_delivery import (
    IDLE_MEASURED_W,
    NonDeliveryState,
    delivering_command,
    evaluate_non_delivery,
)
from .pd_controller import PDState, note_accepted_command, reanchor_pd_state

if TYPE_CHECKING:
    from .pd_controller import PDParams

# A zero command that the device reports, while measured power is at least
# this far from idle, has escaped the idle command and may be reasserted.
_ESCAPE_IDLE_W: Final = IDLE_MEASURED_W


@dataclass(frozen=True)
class SubmitResult:
    """What the ledger will hand to the transport on this call."""

    send_w: int | None
    queued: bool
    skipped: bool


@dataclass(frozen=True)
class ActuatorDecision:
    """Whether the reported device command should be written again.

    ``action`` is ``write``, ``reassert``, ``skip``, or ``wait``.
    ``bypass_interval`` is set for a safety zero or a newly blocking release.
    """

    action: str
    command_w: int
    bypass_interval: bool


class CommandLedger:
    """One in-flight write, one replaceable pending command, one accepted value."""

    def __init__(self, *, accepted_w: int = 0) -> None:
        self.accepted_w = int(accepted_w)
        self.in_flight_w: int | None = None
        self.pending_w: int | None = None
        self.pending_safety: bool = False
        self.last_success_mono: float | None = None
        self.delivery_uncertain: bool = False
        self.uncertain_command_w: int | None = None
        self.anchored_from_measurement: bool = False
        self.non_delivery = NonDeliveryState()
        # Readback observed while a write is awaiting completion. It must not
        # clear ``in_flight_w``. The flight that finishes successfully is newer
        # than this sample and drops it.
        self._deferred_report: tuple[int, PDState | None] | None = None

    def submit(
        self,
        command_w: int,
        *,
        safety: bool = False,
        reassert: bool = False,
    ) -> SubmitResult:
        """Queue or start one command. Newest pending value replaces older ones."""
        command = int(command_w)
        if self.in_flight_w is not None:
            self.pending_w = command
            self.pending_safety = safety or reassert
            return SubmitResult(send_w=None, queued=True, skipped=False)

        same_as_accepted = (
            command == self.accepted_w
            and not safety
            and not reassert
            and not self.delivery_uncertain
        )
        if same_as_accepted:
            self.pending_w = None
            self.pending_safety = False
            return SubmitResult(send_w=None, queued=False, skipped=True)

        self.pending_w = None
        self.pending_safety = False
        self.in_flight_w = command
        return SubmitResult(send_w=command, queued=False, skipped=False)

    def succeed(self, now_mono: float, pd_state: PDState | None = None) -> int:
        """Record that the in-flight command was accepted by the transport."""
        if self.in_flight_w is None:
            raise RuntimeError("no in-flight command to accept")
        self._deferred_report = None
        sent = self.in_flight_w
        self.accepted_w = sent
        self.in_flight_w = None
        self.last_success_mono = now_mono
        self.delivery_uncertain = False
        self.uncertain_command_w = None
        if (
            self.anchored_from_measurement
            and pd_state is not None
            and sent == pd_state.requested_w
        ):
            self.anchored_from_measurement = False
        if pd_state is not None:
            note_accepted_command(pd_state, sent)
        return sent

    def fail(self) -> None:
        """The transport refused the in-flight command.

        The refused command is not accepted. A readback that arrived during
        the await is adopted, because that write did not land.
        """
        if self.in_flight_w is None:
            raise RuntimeError("no in-flight command to fail")
        self.in_flight_w = None
        deferred = self._deferred_report
        self._deferred_report = None
        if deferred is not None:
            self._adopt_reported(deferred[0], deferred[1])

    def note_cancelled(self, command_w: int, *, call_started: bool) -> None:
        """Free the in-flight slot after cancellation without accepting it.

        If the external call had already started, the device may have applied
        the command. That is recorded as uncertain so a later readback or an
        explicit safety zero can reconcile it. The cancelled command is not
        stored as accepted.
        """
        self.in_flight_w = None
        self._deferred_report = None
        if call_started:
            self.delivery_uncertain = True
            self.uncertain_command_w = int(command_w)

    def discard_pending(self) -> None:
        """Drop a queued command. An in-flight write is left running."""
        self.pending_w = None
        self.pending_safety = False

    def reanchor_to_measurement(self) -> None:
        """Forget queued work from the old reference. Do not accept a measurement."""
        self.anchored_from_measurement = True
        self.discard_pending()

    def reconcile_reported(
        self,
        reported_command_w: int,
        pd_state: PDState | None = None,
    ) -> None:
        """Adopt a command read back from the device and clear uncertainty.

        A readback that arrives while a transport call is still awaiting
        completion is stored and does not touch the in-flight slot. Otherwise
        ``succeed`` would find nothing to accept. A successful completion is
        newer than that readback and drops it. A failed completion adopts it,
        because the write did not land.
        """
        reported = int(reported_command_w)
        if self.in_flight_w is not None:
            self._deferred_report = (reported, pd_state)
            return
        self._adopt_reported(reported, pd_state)

    def _adopt_reported(self, reported_command_w: int, pd_state: PDState | None) -> None:
        """Store a device readback. Does not touch an in-flight write."""
        self.accepted_w = int(reported_command_w)
        self.delivery_uncertain = False
        self.uncertain_command_w = None
        if pd_state is not None:
            note_accepted_command(pd_state, self.accepted_w)


def decide_actuator_write(
    *,
    requested_w: int,
    reported_command_w: int,
    measured_power_w: float | None,
    measured_fresh: bool,
    now_mono: float,
    last_change_mono: float | None,
    actuator_latency_s: float,
) -> ActuatorDecision:
    """Compare the request with the command the device already reports.

    A difference is written at the next eligible opportunity. A match is
    skipped when delivery is sufficient, left alone while the actuator is
    still settling, and reasserted when delivery stays insufficient. A zero
    that is already reported is not repeated while the battery is idle.
    """
    requested = int(requested_w)
    reported = int(reported_command_w)
    settling = (
        last_change_mono is not None
        and (now_mono - last_change_mono) < actuator_latency_s
    )
    if requested != reported:
        return ActuatorDecision(
            action="write",
            command_w=requested,
            bypass_interval=requested == 0,
        )

    if requested == 0:
        if settling:
            return ActuatorDecision(action="wait", command_w=0, bypass_interval=False)
        escaped = (
            measured_fresh
            and measured_power_w is not None
            and abs(float(measured_power_w)) >= _ESCAPE_IDLE_W
        )
        if escaped:
            return ActuatorDecision(action="reassert", command_w=0, bypass_interval=True)
        return ActuatorDecision(action="skip", command_w=0, bypass_interval=False)

    if not measured_fresh or measured_power_w is None:
        return ActuatorDecision(action="wait", command_w=requested, bypass_interval=False)

    if settling:
        return ActuatorDecision(action="wait", command_w=requested, bypass_interval=False)

    if delivering_command(requested, float(measured_power_w)):
        return ActuatorDecision(action="skip", command_w=requested, bypass_interval=False)

    return ActuatorDecision(action="reassert", command_w=requested, bypass_interval=False)


def consider_battery_delivery(
    ledger: CommandLedger,
    pd_state: PDState,
    params: PDParams,
    *,
    measured_power_w: float | None,
    measured_fresh: bool,
    now_mono: float,
    reported_command_w: int | None = None,
    publication_id: str | None = None,
    source_mono: float | None = None,
    consecutive_required: int = 3,
    fallback_timeout_s: float = 15.0,
) -> NonDeliveryResult:
    """Count non-delivery of the configured command and re-anchor when required.

    Re-anchor updates the controller request and direction from the measurement
    and discards pending work. It does not change ``accepted_w``, does not
    cancel an in-flight write, and does not release that write's replacement.
    """
    command = (
        int(reported_command_w)
        if reported_command_w is not None
        else ledger.accepted_w
    )
    result = evaluate_non_delivery(
        ledger.non_delivery,
        accepted_command_w=command,
        measured_power_w=measured_power_w,
        measured_fresh=measured_fresh,
        now_mono=now_mono,
        last_write_mono=ledger.last_success_mono,
        actuator_latency_s=params.battery_actuator_latency_s,
        consecutive_required=consecutive_required,
        fallback_timeout_s=fallback_timeout_s,
        publication_id=publication_id,
        source_mono=source_mono,
    )
    if result.reanchor and result.measured_w is not None:
        reanchor_pd_state(pd_state, result.measured_w)
        ledger.reanchor_to_measurement()
    return result
