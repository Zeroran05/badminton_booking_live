import asyncio
import logging
from datetime import datetime, timedelta

import main
from models import BookingAttemptResult, BookingError, BookingPlan, BookingStatus


class FakePage:
    def __init__(self, name: str) -> None:
        self.name = name

    def is_closed(self) -> bool:
        return False


def parallel_plan() -> BookingPlan:
    return BookingPlan(
        "2026-11-13",
        [(3, "15:00-16:00"), (3, "16:00-17:00")],
        "afternoon:same_venue",
    )


def test_both_prearmed_lanes_probe_release_independently(monkeypatch) -> None:
    primary = FakePage("primary")
    secondary = FakePage("secondary")
    events: list[str] = []

    async def open_secondary(*args, **kwargs):
        return secondary

    async def prepare(*args, **kwargs):
        return None

    async def probe(*args, **kwargs):
        lane = args[7]
        events.append(f"lane-{lane}")
        return BookingAttemptResult(
            True,
            3,
            "15:00-16:00" if lane == 1 else "16:00-17:00",
            "2026-11-13",
            "success",
            0,
        ), 12 if lane == 1 else 11

    monkeypatch.setattr(main, "open_parallel_booking_form", open_secondary)
    monkeypatch.setattr(main, "prepare_booking_selection", prepare)
    monkeypatch.setattr(main, "submit_until_release_opens", probe)

    results = asyncio.run(
        main.attempt_parallel_pair(
            None,
            primary,
            parallel_plan(),
            "2026-11-13",
            "13800000000",
            {},
            logging.getLogger("parallel-test"),
            "user",
            "password",
            datetime.now().astimezone() + timedelta(seconds=10),
            0.5,
        )
    )

    assert sorted(events) == ["lane-1", "lane-2"]
    assert results is not None
    assert results[0][3].success
    assert results[1][3].success


def test_second_lane_timeout_does_not_discard_first_success(monkeypatch) -> None:
    primary = FakePage("primary")
    secondary = FakePage("secondary")

    async def open_secondary(*args, **kwargs):
        return secondary

    async def prepare(*args, **kwargs):
        return None

    async def probe(*args, **kwargs):
        lane = args[7]
        if lane == 2:
            raise BookingError(
                BookingStatus.NETWORK_ERROR, "Submission response timed out"
            )
        return BookingAttemptResult(
            True, 3, "15:00-16:00", "2026-11-13", "success", 0
        ), 12

    async def not_confirmed(*args, **kwargs):
        return False

    monkeypatch.setattr(main, "open_parallel_booking_form", open_secondary)
    monkeypatch.setattr(main, "prepare_booking_selection", prepare)
    monkeypatch.setattr(main, "submit_until_release_opens", probe)
    monkeypatch.setattr(main, "confirm_booking_in_progress", not_confirmed)

    results = asyncio.run(
        main.attempt_parallel_pair(
            None,
            primary,
            parallel_plan(),
            "2026-11-13",
            "13800000000",
            {},
            logging.getLogger("parallel-test"),
            "user",
            "password",
            datetime.now().astimezone() + timedelta(seconds=10),
            0.5,
        )
    )

    assert results is not None
    assert results[0][3].success
    assert not results[1][3].success
    assert "Submission response timed out" in results[1][3].message
