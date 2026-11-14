import asyncio
import logging
from datetime import datetime, timedelta

import main
from live_booking import SubmissionRequestTemplate, submit_booking_via_api
from models import BookingAttemptResult, BookingPlan


def test_api_parallel_submits_both_hours_after_gate_probe(monkeypatch, tmp_path) -> None:
    page = object()
    plan = BookingPlan(
        "2026-11-15",
        [(5, "10:00-11:00"), (5, "11:00-12:00")],
        "morning:same_venue",
    )
    submitted: list[str] = []

    async def prepare(*args, **kwargs):
        return None

    async def initial_submit(*args, **kwargs):
        kwargs["request_template_sink"].append(
            SubmissionRequestTemplate(
                "https://example.test/confirmBdmInfo",
                {"content-type": "application/json;charset=UTF-8"},
                {"bookingDate": "2026-11-15", "bookingTime": "10:00:00"},
            )
        )
        return BookingAttemptResult(
            False,
            5,
            "10:00-11:00",
            "2026-11-15",
            "次日场地每天 8:00 起开放预约，请 8:00 后再试",
            500,
        )

    async def api_submit(page, template, venue, target_date, slot, logger):
        submitted.append(slot)
        return BookingAttemptResult(True, venue, slot, target_date, "success", 0)

    monkeypatch.setattr(main, "prepare_booking_selection", prepare)
    monkeypatch.setattr(main, "submit_selected_booking", initial_submit)
    monkeypatch.setattr(main, "submit_booking_via_api", api_submit)

    results = asyncio.run(
        main.attempt_api_parallel_pair(
            page,
            plan,
            "2026-11-15",
            "13800000000",
            logging.getLogger("api-parallel-test"),
            datetime.now().astimezone() + timedelta(seconds=5),
            0.1,
        )
    )

    assert results is not None
    assert sorted(submitted) == ["10:00-11:00", "11:00-12:00"]
    assert all(item[3].success for item in results)


class FakeApiRequest:
    def __init__(self) -> None:
        self.payload = None

    async def post(self, url, data, headers, timeout):
        self.payload = data

        class Response:
            async def json(self):
                return {"code": 0, "msg": "success"}

        return Response()


class FakeContext:
    def __init__(self) -> None:
        self.request = FakeApiRequest()


class FakePage:
    def __init__(self) -> None:
        self.context = FakeContext()


def test_api_submission_clones_template_and_changes_only_target_time() -> None:
    page = FakePage()
    template = SubmissionRequestTemplate(
        "https://example.test/confirmBdmInfo",
        {"authorization": "memory-only"},
        {
            "bdmId": "venue-id",
            "bookingDate": "2026-11-15",
            "bookingTime": "10:00:00",
        },
    )

    result = asyncio.run(
        submit_booking_via_api(
            page,
            template,
            5,
            "2026-11-16",
            "11:00-12:00",
            logging.getLogger("api-template-test"),
        )
    )

    assert result.success
    assert page.context.request.payload["bookingDate"] == "2026-11-16"
    assert page.context.request.payload["bookingTime"] == "11:00:00"
    assert template.payload["bookingTime"] == "10:00:00"
