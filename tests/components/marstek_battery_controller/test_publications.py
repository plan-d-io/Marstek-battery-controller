"""Publication identity, freshness, and one-shot grid integration."""

from __future__ import annotations

from custom_components.marstek_battery_controller.pd_controller import (
    PDParams,
    PDState,
    control_grid,
)
from custom_components.marstek_battery_controller.publications import PublicationTracker


def test_duplicate_publication_does_not_integrate_twice() -> None:
    tracker = PublicationTracker()
    state = PDState()
    assert tracker.publish(
        "grid", 500.0, source_mono=10.0, receive_mono=10.0, publication_id="a"
    ).new_feedback
    first = control_grid(state, PDParams(), tracker, now_mono=10.0)
    assert first.integrate_applied
    assert tracker.publish(
        "grid", 500.0, source_mono=12.0, receive_mono=12.0, publication_id="a"
    ).new_feedback is False
    second = control_grid(state, PDParams(), tracker, now_mono=12.0)
    assert second.integrate_applied is False
    assert state.last_integrate_mono == 10.0


def test_new_publication_integrates_once() -> None:
    tracker = PublicationTracker()
    state = PDState()
    tracker.publish("grid", 100.0, source_mono=1.0, receive_mono=1.0, publication_id="1")
    control_grid(state, PDParams(), tracker, now_mono=1.0)
    assert tracker.publish(
        "grid", 400.0, source_mono=3.0, receive_mono=3.0, publication_id="2"
    ).new_feedback
    assert tracker.should_integrate_grid(3.0)
    second = control_grid(state, PDParams(), tracker, now_mono=3.0)
    assert second.integrate_applied
    assert state.last_publication_id == "2"


def test_soc_update_does_not_integrate_grid() -> None:
    tracker = PublicationTracker()
    state = PDState()
    tracker.publish("grid", 500.0, source_mono=1.0, receive_mono=1.0, publication_id="g")
    control_grid(state, PDParams(), tracker, now_mono=1.0)
    tracker.publish("soc", 55.0, source_mono=2.0, receive_mono=2.0, publication_id="soc")
    tracker.publish("battery", 10.0, source_mono=2.0, receive_mono=2.0, publication_id="b")
    again = control_grid(state, PDParams(), tracker, now_mono=2.0)
    assert again.integrate_applied is False
    assert state.last_integrate_mono == 1.0


def test_wall_age_stale_sample_is_not_feedback() -> None:
    tracker = PublicationTracker(grid_fresh_s=5.0)
    result = tracker.publish(
        "grid",
        500.0,
        source_mono=100.0,
        receive_mono=100.0,
        publication_id="stale",
        wall_age_s=20.0,
    )
    assert result.recorded
    assert result.new_feedback is False
    assert tracker.grid_is_fresh(100.0) is False
    assert tracker.should_integrate_grid(100.0) is False
    stepped = control_grid(PDState(), PDParams(), tracker, now_mono=100.0)
    assert stepped.integrate_applied is False
    assert stepped.reason_code == "stale_input"


def test_older_sample_does_not_replace_a_newer_one() -> None:
    tracker = PublicationTracker()
    tracker.publish("grid", 100.0, source_mono=10.0, receive_mono=10.0, publication_id="new")
    older = tracker.publish(
        "grid", 999.0, source_mono=4.0, receive_mono=11.0, publication_id="old"
    )
    assert older.recorded is False
    assert tracker.grid.value == 100.0
    assert tracker.grid.publication_id == "new"


def test_cadence_extends_freshness_for_a_slow_meter() -> None:
    tracker = PublicationTracker(grid_fresh_s=2.0)
    tracker.publish("grid", 1.0, source_mono=0.0, receive_mono=0.0, publication_id="a")
    tracker.publish("grid", 2.0, source_mono=4.0, receive_mono=4.0, publication_id="b")
    # Observed period is 4 s, so the limit becomes max(2, 12). Age 6 s is fresh.
    assert tracker.grid_is_fresh(10.0)


def test_ev_publication_stays_usable_without_a_freshness_window() -> None:
    tracker = PublicationTracker()
    recorded = tracker.publish(
        "ev", 7000.0, source_mono=1.0, receive_mono=1.0, publication_id="ev-1"
    )
    assert recorded.recorded
    assert recorded.new_feedback
    assert tracker.ev.value == 7000.0
    assert tracker.ev_is_usable()

    repeated = tracker.publish(
        "ev", 100.0, source_mono=80.0, receive_mono=80.0, publication_id="ev-1"
    )
    assert repeated.recorded is False
    assert repeated.new_feedback is False
    assert tracker.ev.value == 7000.0

    older = tracker.publish(
        "ev", 50.0, source_mono=0.5, receive_mono=90.0, publication_id="ev-old"
    )
    assert older.recorded is False
    assert tracker.ev.value == 7000.0
    assert tracker.ev.publication_id == "ev-1"

    assert tracker.ev_is_usable()
    assert tracker.ev.age_s(5.0) == 4.0
    assert tracker.ev.age_s(120.0) == 119.0
    assert tracker.ev_is_usable()

    tracker.mark_unavailable("ev")
    assert tracker.ev_is_usable() is False
    assert tracker.ev.value is None
    assert tracker.ev.available is False

    restored = tracker.publish(
        "ev", 4000.0, source_mono=130.0, receive_mono=130.0, publication_id="ev-2"
    )
    assert restored.recorded
    assert tracker.ev_is_usable()
    assert tracker.ev.value == 4000.0
    assert tracker.ev.age_s(190.0) == 60.0


def test_ev_channel_does_not_change_grid_battery_or_soc_freshness() -> None:
    tracker = PublicationTracker()
    tracker.publish("grid", 10.0, source_mono=0.0, receive_mono=0.0, publication_id="g")
    tracker.publish("battery", 20.0, source_mono=0.0, receive_mono=0.0, publication_id="b")
    tracker.publish("soc", 40.0, source_mono=0.0, receive_mono=0.0, publication_id="s")
    tracker.publish("ev", 7000.0, source_mono=0.0, receive_mono=0.0, publication_id="e")

    assert tracker.grid_is_fresh(5.0)
    assert tracker.grid_is_fresh(5.1) is False
    assert tracker.battery_is_fresh(10.0)
    assert tracker.battery_is_fresh(10.1) is False
    assert tracker.soc_is_fresh(30.0)
    assert tracker.soc_is_fresh(30.1) is False
    assert tracker.ev_is_usable()
    assert tracker.ev.age_s(120.0) == 120.0


def test_unknown_channel_is_rejected() -> None:
    tracker = PublicationTracker()
    try:
        tracker.publish("pv", 1.0, source_mono=0.0, receive_mono=0.0)
    except ValueError as err:
        assert "pv" in str(err)
    else:
        raise AssertionError("expected ValueError")
