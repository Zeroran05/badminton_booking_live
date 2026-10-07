from datetime import date

import pytest

from availability import build_availability, classify_slot, normalize_slot
from booking_page import submission_blocked
from models import Availability, SlotStatus
from strategy import choose_continuation_plan, choose_plan
from browser import NetworkObserver
from live_booking import has_consecutive_pair, slot_to_input_value
from scheduler import next_daily_run
from datetime import datetime
from zoneinfo import ZoneInfo


GROUPS = [
    {"name": "afternoon", "priority": 1, "slots": ["15:00-16:00", "16:00-17:00"]},
    {"name": "evening_early", "priority": 2, "slots": ["19:00-20:00", "20:00-21:00"]},
    {"name": "evening_late", "priority": 3, "slots": ["20:00-21:00", "21:00-22:00"]},
    {"name": "morning", "priority": 4, "slots": ["10:00-11:00", "11:00-12:00"]},
]
VENUES = [2, 3, 1, 4, 5, 6]


def record(venue: int, available: set[str]) -> Availability:
    return Availability(
        venue,
        f"{venue}号羽毛球场",
        "2026-09-24",
        {slot: SlotStatus.AVAILABLE for slot in available},
    )


def test_consecutive_pair_beats_single_hour_in_higher_priority_window() -> None:
    plan = choose_plan(
        [record(6, {"15:00-16:00"}), record(2, {"19:00-20:00", "20:00-21:00"})],
        GROUPS,
        VENUES,
    )
    assert plan is not None
    assert plan.selections == [(2, "19:00-20:00"), (2, "20:00-21:00")]


def test_evening_late_pair_is_accepted() -> None:
    plan = choose_plan(
        [record(2, {"20:00-21:00", "21:00-22:00"})],
        GROUPS,
        VENUES,
    )
    assert plan is not None
    assert plan.selections == [(2, "20:00-21:00"), (2, "21:00-22:00")]


def test_single_hour_keeps_time_then_venue_priority() -> None:
    plan = choose_plan(
        [record(6, {"15:00-16:00"}), record(2, {"20:00-21:00"})],
        GROUPS,
        VENUES,
    )
    assert plan is not None
    assert plan.selections == [(6, "15:00-16:00")]


def test_same_venue_consecutive_is_preferred() -> None:
    plan = choose_plan(
        [record(2, {"15:00-16:00"}), record(3, {"15:00-16:00", "16:00-17:00"})],
        GROUPS,
        VENUES,
    )
    assert plan is not None
    assert plan.selections == [(3, "15:00-16:00"), (3, "16:00-17:00")]


def test_different_venues_can_make_consecutive_pair() -> None:
    plan = choose_plan(
        [record(2, {"15:00-16:00"}), record(3, {"16:00-17:00"})],
        GROUPS,
        VENUES,
    )
    assert plan is not None
    assert plan.selections == [(2, "15:00-16:00"), (3, "16:00-17:00")]


def test_continuation_uses_another_venue_before_other_time_windows() -> None:
    plan = choose_continuation_plan(
        [
            record(2, {"10:00-11:00", "11:00-12:00"}),
            record(4, {"16:00-17:00"}),
        ],
        ["16:00-17:00"],
        VENUES,
        preferred_venue=2,
    )
    assert plan is not None
    assert plan.selections == [(4, "16:00-17:00")]


def test_continuation_prefers_the_original_venue() -> None:
    plan = choose_continuation_plan(
        [record(2, {"16:00-17:00"}), record(6, {"16:00-17:00"})],
        ["16:00-17:00"],
        VENUES,
        preferred_venue=6,
    )
    assert plan is not None
    assert plan.selections == [(6, "16:00-17:00")]


def test_early_stop_detects_highest_priority_pair() -> None:
    records = [
        record(2, {"15:00-16:00"}),
        record(3, {"16:00-17:00"}),
    ]
    assert has_consecutive_pair(
        records,
        ["15:00-16:00", "16:00-17:00"],
        VENUES,
        allow_different_venues=True,
    )
    assert not has_consecutive_pair(
        records,
        ["15:00-16:00", "16:00-17:00"],
        VENUES,
        allow_different_venues=False,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("08点-09点", "08:00-09:00"), ("15:00至16:00", "15:00-16:00"), ("21时—22时", "21:00-22:00")],
)
def test_normalize_slot(raw: str, expected: str) -> None:
    assert normalize_slot(raw) == expected


def test_status_uses_semantics_not_color() -> None:
    assert classify_slot({"text": "15点-16点", "attrs": {"aria-disabled": "true"}}) == SlotStatus.DISABLED
    assert classify_slot({"text": "15点-16点 可预约", "attrs": {"style": "color:gray"}}) == SlotStatus.AVAILABLE


def test_build_availability_always_has_all_fourteen_slots() -> None:
    result = build_availability(2, date(2026, 9, 24), [{"text": "15点-16点 可预约", "attrs": {}, "interactive": True}])
    assert len(result.slots) == 14
    assert result.slots["15:00-16:00"] == SlotStatus.AVAILABLE


def test_submission_is_unconditionally_blocked() -> None:
    with pytest.raises(RuntimeError, match="blocked in dry-run"):
        submission_blocked()


def test_network_log_strips_query_values(tmp_path) -> None:
    class FakeRequest:
        resource_type = "xhr"
        method = "GET"
        url = "https://example.test/availability?token=secret&venue=2"
        post_data = None

    import logging

    observer = NetworkObserver(logging.getLogger("test"), tmp_path / "network.json")
    observer.on_request(FakeRequest())
    assert observer.events[0]["url"] == "https://example.test/availability"
    assert observer.events[0]["parameter_names"] == ["venue"]


def test_slot_to_input_value() -> None:
    assert slot_to_input_value("15:00-16:00") == "15:00:00"


def test_scheduler_uses_next_day_after_release() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 9, 23, 8, 0, 1, tzinfo=timezone)
    target = next_daily_run(now, "08:00:00")
    assert target == datetime(2026, 9, 24, 8, 0, 0, tzinfo=timezone)


def test_scheduler_run_now() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 9, 23, 19, 0, 0, tzinfo=timezone)
    assert next_daily_run(now, "08:00:00", run_now=True) == now
