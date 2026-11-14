import asyncio
import logging

from live_booking import submit_selected_booking


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def json(self) -> dict:
        return self.payload


class FakeResponseInfo:
    def __init__(self, payload: dict) -> None:
        self.response = FakeResponse(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    @property
    def value(self):
        async def get_response():
            return self.response

        return get_response()


class FakeSubmit:
    @property
    def first(self):
        return self

    def filter(self, **kwargs):
        return self

    async def count(self) -> int:
        return 1

    async def is_enabled(self) -> bool:
        return True

    async def click(self) -> None:
        return None


class FakePage:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.submit = FakeSubmit()
        self.screenshots: list[str] = []
        self.waits: list[int] = []
        self.listeners: dict[str, list] = {}

    def on(self, event: str, callback) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event: str, callback) -> None:
        if callback in self.listeners.get(event, []):
            self.listeners[event].remove(callback)

    def locator(self, selector: str) -> FakeSubmit:
        assert selector == "button"
        return self.submit

    def expect_response(self, predicate, timeout: int) -> FakeResponseInfo:
        assert timeout == 2_000
        return FakeResponseInfo(self.payload)

    def expect_request(self, predicate, timeout: int) -> FakeResponseInfo:
        assert timeout == 300
        return FakeResponseInfo(self.payload)

    async def screenshot(self, path: str, full_page: bool) -> None:
        assert full_page
        self.screenshots.append(path)

    async def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)


def test_repeated_gate_probe_skips_screenshots_and_fixed_wait(tmp_path) -> None:
    page = FakePage(
        {"code": 500, "msg": "次日场地每天 8:00 起开放预约，请 8:00 后再试"}
    )

    result = asyncio.run(
        submit_selected_booking(
            page,
            2,
            "2026-11-12",
            "15:00-16:00",
            tmp_path,
            logging.getLogger("test"),
            capture_before=False,
            capture_gate_rejection=False,
        )
    )

    assert not result.success
    assert page.screenshots == []
    assert page.waits == []


def test_success_still_keeps_result_evidence_and_settle_wait(tmp_path) -> None:
    page = FakePage({"code": 0, "msg": "success"})

    result = asyncio.run(
        submit_selected_booking(
            page,
            2,
            "2026-11-12",
            "15:00-16:00",
            tmp_path,
            logging.getLogger("test"),
            capture_before=False,
            capture_gate_rejection=False,
        )
    )

    assert result.success
    assert len(page.screenshots) == 1
    assert page.waits == [500]
