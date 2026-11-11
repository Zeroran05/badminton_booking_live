import asyncio

import pytest

from models import BookingError, BookingStatus
from portal import _wait_for_booking_target


class FakeLocator:
    def __init__(self, count: int = 0) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class FakeFrame:
    def __init__(self, has_booking_form: bool = False) -> None:
        self.has_booking_form = has_booking_form

    def get_by_text(self, text: str, exact: bool = False) -> FakeLocator:
        return FakeLocator(1 if self.has_booking_form and text == "联系方式" else 0)


class FakePage:
    def __init__(self, url: str, has_booking_form: bool = False) -> None:
        self.url = url
        self.frames = [FakeFrame(has_booking_form)]
        self.load_waits = 0

    async def wait_for_load_state(self, state: str, timeout: int) -> None:
        assert state == "domcontentloaded"
        assert timeout > 0
        self.load_waits += 1


class DelayedPopupContext:
    def __init__(self, portal: FakePage, popup: FakePage, delay_reads: int) -> None:
        self.portal = portal
        self.popup = popup
        self.delay_reads = delay_reads
        self.reads = 0

    @property
    def pages(self) -> list[FakePage]:
        self.reads += 1
        if self.reads <= self.delay_reads:
            return [self.portal]
        return [self.portal, self.popup]


def test_waits_for_delayed_popup_instead_of_reusing_portal() -> None:
    portal = FakePage("https://portal.example/dashboard")
    popup = FakePage("https://booking.example/form")
    context = DelayedPopupContext(portal, popup, delay_reads=3)

    target, mode = asyncio.run(
        _wait_for_booking_target(
            context,
            portal,
            [portal],
            portal.url,
            timeout_seconds=0.2,
            poll_interval_seconds=0.001,
        )
    )

    assert target is popup
    assert mode == "new_tab"
    assert popup.load_waits == 1


def test_accepts_same_url_only_after_booking_form_is_visible() -> None:
    portal = FakePage("https://portal.example/dashboard", has_booking_form=True)
    context = DelayedPopupContext(portal, FakePage("about:blank"), delay_reads=999)

    target, mode = asyncio.run(
        _wait_for_booking_target(
            context,
            portal,
            [portal],
            portal.url,
            timeout_seconds=0.1,
            poll_interval_seconds=0.001,
        )
    )

    assert target is portal
    assert mode == "spa_or_current_page"


def test_does_not_mistake_unchanged_portal_for_booking_service() -> None:
    portal = FakePage("https://portal.example/dashboard")
    context = DelayedPopupContext(portal, FakePage("about:blank"), delay_reads=999)

    with pytest.raises(BookingError) as caught:
        asyncio.run(
            _wait_for_booking_target(
                context,
                portal,
                [portal],
                portal.url,
                timeout_seconds=0.01,
                poll_interval_seconds=0.001,
            )
        )

    assert caught.value.status == BookingStatus.PAGE_STRUCTURE_CHANGED
