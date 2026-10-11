"""Battery-only operating policy and one PD control step.

Home Assistant stays outside this module. The coordinator publishes meter
samples and performs the Modbus write; the session decides the command.

Self-consumption, Boost and Reserve share the PD controller. Boost's fixed
charge and Manual's fixed power are operating policy, not a second controller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Literal

from . import const
from .calculator import boost_window_active, protection_window_active
from .command_ledger import (
    CommandLedger,
    consider_battery_delivery,
    decide_actuator_write,
)
from .pd_controller import (
    DEFAULT_DEADBAND_W,
    PD_DEADBAND_MAX_W,
    PD_DEADBAND_MIN_W,
    PD_DEADBAND_STEP_W,
    PDParams,
    PDState,
    control_grid,
    reanchor_pd_state,
    reset_pd_state,
)
from .publications import PublicationTracker

_NEUTRAL_FORCE_MODES = frozenset({"stop", "standby", "neutral", "off", "idle"})
_PD_MODES = frozenset(
    {
        const.MODE_SELF_CONSUMPTION,
        const.MODE_SELF_CONSUMPTION_BOOST,
        const.MODE_SELF_CONSUMPTION_RESERVE,
    }
)
_FROZEN_KINDS = frozenset({"boost_fixed", "manual", "released"})


def normalize_pd_grid_deadband(value: float) -> float:
    """Snap a deadband onto the 0–200 W, 5 W entity grid. 0 disables it."""
    step = PD_DEADBAND_STEP_W
    snapped = round(float(value) / step) * step
    if snapped < PD_DEADBAND_MIN_W:
        return PD_DEADBAND_MIN_W
    if snapped > PD_DEADBAND_MAX_W:
        return PD_DEADBAND_MAX_W
    return float(snapped)


def reported_command_w(
    force_mode: str | None,
    charge_w: float | None,
    discharge_w: float | None,
) -> int | None:
    """Rebuild the signed command from the Marstek force mode and setpoints.

    Neutral, stop and standby are 0 W. Force discharge is the positive
    discharge setpoint. Force charge is the negative charge setpoint.
    A missing setpoint for an active charge or discharge mode is unknown,
    not 0 W.
    """
    if force_mode is None:
        return None
    mode = force_mode.strip().lower()
    if mode in _NEUTRAL_FORCE_MODES:
        return 0
    if mode == const.FORCE_DISCHARGE:
        if discharge_w is None:
            return None
        return max(0, int(round(float(discharge_w))))
    if mode == const.FORCE_CHARGE:
        if charge_w is None:
            return None
        return -max(0, int(round(float(charge_w))))
    return None


@dataclass(frozen=True)
class SessionSettings:
    """One coordinator observation, already parsed from Home Assistant state."""

    mode: str
    soc: float
    soc_sensor_ok: bool
    min_soc: float
    max_soc: float
    max_battery_power_w: float
    min_interval_s: float
    deadband_w: float
    reserve_target_soc: float
    boost_charge_power_w: float
    manual_target_soc: float
    manual_power_w: float
    reserve_protection_start: time
    peak_window_start: time
    capacity_tariff_enabled: bool
    cap_now: float
    effective_cap_w: float
    latest_start_charge: datetime | Literal["no_need"]
    peak_window_deadline: datetime
    now: datetime
    # False when SoC has no fresh publication. Permissions must not use it.
    soc_fresh: bool = True


@dataclass(frozen=True)
class ReportedObservation:
    """One coherent device readback, or an explicit unknown."""

    command_w: int | None
    publication_id: str | None
    observed_mono: float | None
    fresh: bool
    coherent: bool


@dataclass(frozen=True)
class ChannelSample:
    """One grid or battery observation offered to the publication tracker."""

    value_w: float | None
    publication_id: str | None = None
    source_mono: float | None = None
    wall_age_s: float | None = None
    available: bool = True


@dataclass(frozen=True)
class OperatingPolicy:
    """What the mode allows. The PD request is filled in afterwards."""

    kind: str
    fixed_command_w: int
    allow_charge: bool
    allow_discharge: bool
    operating_state: str
    reason_code: str


@dataclass(frozen=True)
class ControlPlan:
    """The command to write, if any. A proposal is not an accepted command."""

    kind: str
    proposed_w: int
    send_w: int | None
    safety: bool
    reassert: bool
    release: bool
    operating_state: str
    reason_code: str


def _clamp_power(value: float, limit_w: float) -> int:
    limit = abs(float(limit_w))
    return int(round(max(-limit, min(limit, value))))


def operating_policy(settings: SessionSettings) -> OperatingPolicy:
    """Separate mode rules from the PD calculation.

    Plain self-consumption uses PD plus the existing SoC guards. Capacity
    tariff does not add a setpoint of its own there; it keeps its current
    Boost fallback and Reserve discharge exception. Manual bypasses PD and
    the SoC guards. Released resets control.
    """
    allow_discharge = settings.soc > settings.min_soc
    allow_charge = settings.soc < settings.max_soc
    at_floor = settings.soc <= settings.min_soc
    at_ceiling = settings.soc >= settings.max_soc

    if settings.mode == const.MODE_RELEASED:
        return OperatingPolicy(
            kind="released",
            fixed_command_w=0,
            allow_charge=False,
            allow_discharge=False,
            operating_state=const.STATE_RELEASED,
            reason_code=const.REASON_RELEASED,
        )

    if settings.mode == const.MODE_MANUAL:
        target = settings.manual_target_soc
        if round(settings.soc) == round(target):
            command = 0
            state = const.STATE_MANUAL_CHARGING
        elif target > settings.soc:
            command = _clamp_power(-settings.manual_power_w, settings.max_battery_power_w)
            state = const.STATE_MANUAL_CHARGING
        else:
            command = _clamp_power(settings.manual_power_w, settings.max_battery_power_w)
            state = const.STATE_MANUAL_DISCHARGING
        return OperatingPolicy(
            kind="manual",
            fixed_command_w=command,
            allow_charge=True,
            allow_discharge=True,
            operating_state=state,
            reason_code=const.REASON_MANUAL_ACTIVE,
        )

    under_cap = (not settings.capacity_tariff_enabled) or (
        settings.cap_now < settings.effective_cap_w
    )
    cap_blocking = settings.capacity_tariff_enabled and (
        settings.cap_now >= settings.effective_cap_w
    )

    if settings.mode == const.MODE_SELF_CONSUMPTION_BOOST:
        boost_on = boost_window_active(
            settings.latest_start_charge,
            settings.peak_window_deadline,
            settings.now,
        )
        inside = boost_on and settings.soc < settings.max_soc
        if inside and under_cap:
            command = _clamp_power(
                -settings.boost_charge_power_w,
                settings.max_battery_power_w,
            )
            return OperatingPolicy(
                kind="boost_fixed",
                fixed_command_w=command,
                allow_charge=True,
                allow_discharge=allow_discharge,
                operating_state=const.STATE_BOOST_CHARGING,
                reason_code=const.REASON_BOOST_ACTIVE,
            )
        if inside and cap_blocking:
            return _pd_policy(
                allow_charge,
                allow_discharge,
                at_floor,
                at_ceiling,
                state=const.STATE_SELF_CONSUMPTION,
                reason=const.REASON_CAP_TARIFF,
            )

    if settings.mode == const.MODE_SELF_CONSUMPTION_RESERVE:
        if protection_window_active(
            settings.reserve_protection_start,
            settings.peak_window_start,
            settings.now,
        ):
            at_reserve = settings.soc <= settings.reserve_target_soc
            if at_reserve and under_cap:
                return _pd_policy(
                    allow_charge,
                    False,
                    at_floor,
                    at_ceiling,
                    state=const.STATE_RESERVE_HELD,
                    reason=const.REASON_RESERVE_HELD,
                )
            if at_reserve and cap_blocking:
                return _pd_policy(
                    allow_charge,
                    allow_discharge,
                    at_floor,
                    at_ceiling,
                    state=const.STATE_SELF_CONSUMPTION,
                    reason=const.REASON_CAP_TARIFF,
                )

    reason = const.REASON_NORMAL
    if at_floor:
        reason = const.REASON_AT_FLOOR
    elif at_ceiling:
        reason = const.REASON_AT_CEILING
    return _pd_policy(
        allow_charge,
        allow_discharge,
        at_floor,
        at_ceiling,
        state=const.STATE_SELF_CONSUMPTION,
        reason=reason,
    )


def _pd_policy(
    allow_charge: bool,
    allow_discharge: bool,
    at_floor: bool,
    at_ceiling: bool,
    *,
    state: str,
    reason: str,
) -> OperatingPolicy:
    """PD kind. A specific mode reason wins over the generic SoC labels."""
    if at_floor:
        allow_discharge = False
        if reason in (const.REASON_NORMAL, const.REASON_CAP_TARIFF):
            reason = const.REASON_AT_FLOOR
    elif at_ceiling and reason == const.REASON_NORMAL:
        reason = const.REASON_AT_CEILING
    if at_ceiling:
        allow_charge = False
    return OperatingPolicy(
        kind="pd",
        fixed_command_w=0,
        allow_charge=allow_charge,
        allow_discharge=allow_discharge,
        operating_state=state,
        reason_code=reason,
    )


class BatteryControlSession:
    """PD state, publications, and the one-command ledger for one entry."""

    def __init__(self) -> None:
        self.pd = PDState()
        self.ledger = CommandLedger()
        self.publications = PublicationTracker()
        self.kind: str | None = None
        self.release_requested = False
        self._last_reported_id: str | None = None

    def pd_params(self, settings: SessionSettings) -> PDParams:
        """Send interval is the ordinary opportunity gate. Deadband is the entity."""
        return PDParams(
            min_interval_s=float(settings.min_interval_s),
            deadband_w=normalize_pd_grid_deadband(settings.deadband_w),
            max_battery_power_w=float(settings.max_battery_power_w),
        )

    def prepare_release(self) -> None:
        """Drop queued work and PD memory. An in-flight write is left running."""
        self.release_requested = True
        self.ledger.discard_pending()
        reset_pd_state(self.pd)
        self.kind = "released"

    def note_release_finished(self, *, accepted: bool) -> None:
        """Record a finished Released cleanup without adopting a stale command."""
        self.release_requested = False
        if not accepted or self.ledger.in_flight_w is not None:
            return
        self.ledger.accepted_w = 0
        self.ledger.delivery_uncertain = False
        self.ledger.uncertain_command_w = None
        reset_pd_state(self.pd)
        self.kind = "released"

    def consider_reported(self, observation: ReportedObservation, *, now_mono: float) -> bool:
        """Adopt one new coherent readback. Stale or older telemetry is ignored.

        ``now_mono`` is unused; the observation already carries its monotonic time.
        """
        del now_mono
        if (
            not observation.coherent
            or not observation.fresh
            or observation.command_w is None
            or observation.publication_id is None
            or observation.observed_mono is None
            or self.ledger.in_flight_w is not None
        ):
            return False
        if observation.publication_id == self._last_reported_id:
            return False
        if (
            self.ledger.last_success_mono is not None
            and observation.observed_mono + 1e-9 < self.ledger.last_success_mono
        ):
            return False
        pd_state = self.pd if self.kind == "pd" else None
        self.ledger.reconcile_reported(int(observation.command_w), pd_state)
        self._last_reported_id = observation.publication_id
        return True

    def _ingest_reported(
        self,
        observation: ReportedObservation | None,
        reported_command_w: int | None,
        *,
        now_mono: float,
    ) -> int | None:
        """Return the command decide should compare against."""
        if observation is not None:
            self.consider_reported(observation, now_mono=now_mono)
            return self.ledger.accepted_w
        if reported_command_w is None or self.ledger.in_flight_w is not None:
            return None
        if self.kind == "pd":
            self.ledger.reconcile_reported(reported_command_w, self.pd)
        else:
            self.ledger.reconcile_reported(reported_command_w, None)
        return int(reported_command_w)

    def _safety_zero_plan(
        self,
        settings: SessionSettings,
        *,
        now_mono: float,
        battery: ChannelSample,
        reported_for_decide: int | None,
    ) -> ControlPlan:
        """Command zero without using a stale SoC as permission to keep power."""
        reported = (
            self.ledger.accepted_w
            if reported_for_decide is None
            else int(reported_for_decide)
        )
        battery_fresh = self.publications.battery_is_fresh(now_mono)
        decision = decide_actuator_write(
            requested_w=0,
            reported_command_w=reported,
            measured_power_w=None if battery.value_w is None else float(battery.value_w),
            measured_fresh=battery_fresh and battery.available and battery.value_w is not None,
            now_mono=now_mono,
            last_change_mono=self.ledger.last_success_mono,
            actuator_latency_s=self.pd_params(settings).battery_actuator_latency_s,
        )
        send_w = None
        safety = False
        reassert = False
        if decision.action in ("write", "reassert"):
            send_w = decision.command_w
            safety = True
            reassert = decision.action == "reassert"
        return self._plan(
            kind=self.kind or "pd",
            proposed_w=0,
            send_w=send_w,
            safety=safety,
            reassert=reassert,
            release=False,
            operating_state=_state_for_mode(settings.mode),
            reason_code=const.REASON_NORMAL,
        )

    def plan(
        self,
        settings: SessionSettings,
        *,
        now_mono: float,
        grid: ChannelSample,
        battery: ChannelSample,
        reported_command_w: int | None = None,
        reported: ReportedObservation | None = None,
        opportunity: bool = True,
    ) -> ControlPlan:
        """Publish inputs, apply policy, and choose at most one command to send."""
        self._publish("grid", grid, now_mono)
        self._publish("battery", battery, now_mono)
        reported_for_decide = self._ingest_reported(
            reported,
            reported_command_w,
            now_mono=now_mono,
        )

        if settings.mode == const.MODE_RELEASED or settings.mode not in _PD_MODES | {
            const.MODE_MANUAL
        }:
            if settings.mode == const.MODE_RELEASED:
                self.prepare_release()
                return self._plan(
                    kind="released",
                    proposed_w=0,
                    send_w=None,
                    safety=False,
                    reassert=False,
                    release=True,
                    operating_state=const.STATE_RELEASED,
                    reason_code=const.REASON_RELEASED,
                )

        if settings.mode != const.MODE_RELEASED and not (
            settings.soc_sensor_ok and settings.soc_fresh
        ):
            return self._safety_zero_plan(
                settings,
                now_mono=now_mono,
                battery=battery,
                reported_for_decide=reported_for_decide,
            )

        policy = operating_policy(settings)
        if self.kind in _FROZEN_KINDS and policy.kind == "pd":
            self._reanchor_to_battery(now_mono)
        if self.kind is not None and policy.kind != self.kind:
            self.ledger.discard_pending()
        self.kind = policy.kind
        if policy.kind != "released":
            self.release_requested = False

        if policy.kind == "released":
            self.prepare_release()
            return self._plan(
                kind="released",
                proposed_w=0,
                send_w=None,
                safety=False,
                reassert=False,
                release=True,
                operating_state=policy.operating_state,
                reason_code=policy.reason_code,
            )

        params = self.pd_params(settings)
        interval_open = self._interval_open(now_mono, params.min_interval_s)
        stale = False
        if policy.kind == "pd":
            result = control_grid(
                self.pd,
                params,
                self.publications,
                now_mono=now_mono,
                opportunity=opportunity,
                allow_charge=policy.allow_charge,
                allow_discharge=policy.allow_discharge,
            )
            proposed = result.proposed_command_w
            stale = result.reason_code == "stale_input"
        else:
            proposed = policy.fixed_command_w

        if policy.kind == "pd" and not stale:
            battery_fresh = self.publications.battery_is_fresh(now_mono)
            delivery = consider_battery_delivery(
                self.ledger,
                self.pd,
                params,
                measured_power_w=None if battery.value_w is None else float(battery.value_w),
                measured_fresh=battery_fresh and battery.available,
                now_mono=now_mono,
                reported_command_w=reported_for_decide,
                publication_id=battery.publication_id,
                source_mono=self.publications.battery.source_mono,
            )
            if delivery.reanchor:
                proposed = self.pd.requested_w

        reason = policy.reason_code
        operating_state = policy.operating_state
        if reason == const.REASON_AT_FLOOR and proposed < 0:
            reason = const.REASON_NORMAL
        elif reason == const.REASON_AT_CEILING and proposed > 0:
            reason = const.REASON_NORMAL
        elif reason == const.REASON_RESERVE_HELD and proposed < 0:
            # Surplus charging stays allowed. The held label is the discharge block.
            reason = const.REASON_NORMAL
            operating_state = const.STATE_SELF_CONSUMPTION

        reported = (
            int(reported_for_decide)
            if reported_for_decide is not None
            else self.ledger.accepted_w
        )
        battery_fresh = self.publications.battery_is_fresh(now_mono)
        decision = decide_actuator_write(
            requested_w=proposed,
            reported_command_w=reported,
            measured_power_w=None if battery.value_w is None else float(battery.value_w),
            measured_fresh=battery_fresh and battery.available and battery.value_w is not None,
            now_mono=now_mono,
            last_change_mono=self.ledger.last_success_mono,
            actuator_latency_s=params.battery_actuator_latency_s,
        )
        send_w: int | None = None
        safety = False
        reassert = False
        if decision.action in ("write", "reassert") and (
            decision.bypass_interval or interval_open
        ):
            send_w = decision.command_w
            safety = decision.bypass_interval
            reassert = decision.action == "reassert"
            if not decision.bypass_interval:
                self.pd.last_opportunity_mono = now_mono
        if self.ledger.in_flight_w is not None and send_w == self.ledger.in_flight_w:
            send_w = None
            safety = False
            reassert = False
        return self._plan(
            kind=policy.kind,
            proposed_w=proposed,
            send_w=send_w,
            safety=safety,
            reassert=reassert,
            release=False,
            operating_state=operating_state,
            reason_code=reason,
        )

    def _interval_open(self, now_mono: float, min_interval_s: float) -> bool:
        last = self.pd.last_opportunity_mono
        if last is None:
            return True
        return (now_mono - last) + 1e-9 >= min_interval_s

    def _reanchor_to_battery(self, now_mono: float) -> None:
        """Start the next PD step from measured power, not the frozen request."""
        sample = self.publications.battery
        if sample.available and sample.value is not None and sample.is_fresh(
            now_mono,
            self.publications.battery_fresh_s,
        ):
            reanchor_pd_state(self.pd, sample.value)
        else:
            reset_pd_state(self.pd)
        self.ledger.reanchor_to_measurement()

    def _publish(self, channel: str, sample: ChannelSample, now_mono: float) -> None:
        if not sample.available or sample.value_w is None:
            self.publications.mark_unavailable(channel)
            return
        source = now_mono if sample.source_mono is None else float(sample.source_mono)
        self.publications.publish(
            channel,
            float(sample.value_w),
            source_mono=source,
            receive_mono=now_mono,
            publication_id=sample.publication_id,
            wall_age_s=sample.wall_age_s,
        )

    def _plan(
        self,
        *,
        kind: str,
        proposed_w: int,
        send_w: int | None,
        safety: bool,
        reassert: bool,
        release: bool,
        operating_state: str,
        reason_code: str,
    ) -> ControlPlan:
        return ControlPlan(
            kind=kind,
            proposed_w=int(proposed_w),
            send_w=None if send_w is None else int(send_w),
            safety=safety,
            reassert=reassert,
            release=release,
            operating_state=operating_state,
            reason_code=reason_code,
        )


def _state_for_mode(mode: str) -> str:
    if mode == const.MODE_MANUAL:
        return const.STATE_MANUAL_CHARGING
    if mode == const.MODE_RELEASED:
        return const.STATE_RELEASED
    return const.STATE_SELF_CONSUMPTION
