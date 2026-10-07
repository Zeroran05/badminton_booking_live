from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from models import Availability, BookingAttemptResult, BookingError, BookingStatus, SlotStatus


EXPECTED_SLOTS = [f"{hour:02d}:00-{hour + 1:02d}:00" for hour in range(8, 22)]
def is_release_not_open_message(message: str) -> bool:
    """Return True only for the server's temporary release-gate rejection."""
    normalized = re.sub(r"\s+", " ", message).strip()
    mentions_release_time = any(marker in normalized for marker in ("8:00", "8点"))
    return mentions_release_time and "开放预约" in normalized and "再试" in normalized


def slot_to_input_value(slot: str) -> str:
    return slot.split("-", 1)[0] + ":00"


async def _venue_option(page: Page, venue: int):
    locator = page.get_by_text(f"{venue}号羽毛球场", exact=True).first
    if not await locator.count():
        raise BookingError(BookingStatus.VENUE_NOT_FOUND, f"Venue {venue} not found")
    return locator


async def _click_venue_and_wait(page: Page, venue: int) -> None:
    option = await _venue_option(page, venue)
    target_input = option.locator("xpath=ancestor::label[1]//input[@type='radio']")
    if await target_input.count() and await target_input.is_checked():
        # Vue does not emit a change event when the already-selected radio is clicked.
        # Move to another venue first so a fresh availability request is guaranteed.
        for alternate in range(1, 7):
            if alternate == venue:
                continue
            alternate_option = await _venue_option(page, alternate)
            try:
                async with page.expect_response(
                    lambda response: "getOptionalTime" in response.url,
                    timeout=15_000,
                ):
                    await alternate_option.click()
                break
            except PlaywrightTimeoutError:
                continue
    try:
        async with page.expect_response(
            lambda response: "getOptionalTime" in response.url,
            timeout=15_000,
        ):
            await option.click()
    except PlaywrightTimeoutError as exc:
        raise BookingError(
            BookingStatus.NETWORK_ERROR,
            f"Venue {venue} availability request timed out",
        ) from exc
    await page.wait_for_timeout(30)


async def scan_venue(page: Page, venue: int, target_date: str) -> Availability:
    await _click_venue_and_wait(page, venue)
    date_input = page.locator(f'input[type="radio"][value="{target_date}"]')
    if not await date_input.count() or await date_input.is_disabled():
        return Availability(
            venue,
            f"{venue}号羽毛球场",
            target_date,
            {slot: SlotStatus.UNAVAILABLE for slot in EXPECTED_SLOTS},
        )

    await date_input.check()
    if not await date_input.is_checked():
        raise BookingError(
            BookingStatus.TARGET_DATE_NOT_AVAILABLE,
            f"Could not select {target_date} for venue {venue}",
        )

    # Read every time radio in one browser round-trip. The previous per-slot
    # count/disabled/checked calls multiplied latency across 14 slots x 6 venues.
    input_states = await page.locator('input[type="radio"]').evaluate_all(
        """elements => elements.map(element => ({
            value: element.value,
            disabled: element.disabled,
            checked: element.checked
        }))"""
    )
    by_value = {str(item["value"]): item for item in input_states}
    slots: dict[str, SlotStatus] = {}
    for slot in EXPECTED_SLOTS:
        state = by_value.get(slot_to_input_value(slot))
        if state is None:
            slots[slot] = SlotStatus.UNKNOWN
        elif state["disabled"]:
            slots[slot] = SlotStatus.UNAVAILABLE
        elif state["checked"]:
            slots[slot] = SlotStatus.SELECTED
        else:
            slots[slot] = SlotStatus.AVAILABLE
    return Availability(venue, f"{venue}号羽毛球场", target_date, slots)


def has_consecutive_pair(
    records: list[Availability],
    slots: list[str],
    venue_priority: list[int],
    allow_different_venues: bool,
) -> bool:
    selectable = {SlotStatus.AVAILABLE, SlotStatus.SELECTED}
    by_venue = {record.venue: record for record in records}
    if any(
        venue in by_venue
        and all(by_venue[venue].slots.get(slot) in selectable for slot in slots)
        for venue in venue_priority
    ):
        return True
    return allow_different_venues and all(
        any(
            venue in by_venue
            and by_venue[venue].slots.get(slot) in selectable
            for venue in venue_priority
        )
        for slot in slots
    )


async def scan_all_venues(
    page: Page,
    venue_priority: list[int],
    target_date: str,
    logger: logging.Logger,
    completed: set[tuple[int, str]] | None = None,
    early_stop_slots: list[str] | None = None,
    allow_different_venues: bool = True,
) -> list[Availability]:
    started = time.perf_counter()
    records: list[Availability] = []
    completed = completed or set()
    for venue in venue_priority:
        record = await scan_venue(page, venue, target_date)
        # Once an hour has been booked, do not book the same person into another
        # venue during that hour. Continue looking for the complementary hour.
        for _, completed_slot in completed:
            record.slots[completed_slot] = SlotStatus.UNAVAILABLE
        records.append(record)
        summary = [slot for slot, status in record.slots.items() if status == SlotStatus.AVAILABLE]
        logger.info("Venue %d available target slots: %s", venue, summary)
        if early_stop_slots and has_consecutive_pair(
            records,
            early_stop_slots,
            venue_priority,
            allow_different_venues,
        ):
            logger.info("Priority target found; stopping scan early")
            break
    logger.info("Availability scan completed in %.3f seconds", time.perf_counter() - started)
    return records


async def select_booking_choice(page: Page, venue: int, target_date: str, slot: str) -> None:
    await _click_venue_and_wait(page, venue)
    date_input = page.locator(f'input[type="radio"][value="{target_date}"]')
    if not await date_input.count() or await date_input.is_disabled():
        raise BookingError(BookingStatus.TARGET_DATE_NOT_AVAILABLE)
    await date_input.check()

    time_input = page.locator(
        f'input[type="radio"][value="{slot_to_input_value(slot)}"]'
    )
    if not await time_input.count() or await time_input.is_disabled():
        raise BookingError(
            BookingStatus.SUBMISSION_FAILED,
            f"{venue}号场 {slot} became unavailable",
        )
    await time_input.check()
    if not await date_input.is_checked() or not await time_input.is_checked():
        raise BookingError(BookingStatus.PAGE_STRUCTURE_CHANGED, "Booking choice did not remain selected")


async def submit_selected_booking(
    page: Page,
    venue: int,
    target_date: str,
    slot: str,
    root: Path,
    logger: logging.Logger,
) -> BookingAttemptResult:
    submit = page.locator("button").filter(has_text=re.compile(r"提\s*交")).first
    if not await submit.count() or not await submit.is_enabled():
        raise BookingError(BookingStatus.PAGE_STRUCTURE_CHANGED, "Submit button is missing or disabled")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    await page.screenshot(
        path=str(root / "screenshots" / f"before_submit_{venue}_{slot[:2]}_{stamp}.png"),
        full_page=True,
    )
    try:
        async with page.expect_response(
            lambda response: "confirmBdmInfo" in response.url
            and response.request.method == "POST",
            timeout=20_000,
        ) as response_info:
            await submit.click()
        response = await response_info.value
        payload: Any = await response.json()
    except PlaywrightTimeoutError as exc:
        raise BookingError(BookingStatus.NETWORK_ERROR, "Submission response timed out") from exc

    code = payload.get("code") if isinstance(payload, dict) else None
    message = str(payload.get("msg", "")) if isinstance(payload, dict) else "Invalid response"
    success = code == 0
    logger.info(
        "Submission result venue=%d slot=%s code=%s message=%s",
        venue,
        slot,
        code,
        message,
    )
    await page.wait_for_timeout(500)
    await page.screenshot(
        path=str(root / "screenshots" / f"submit_result_{venue}_{slot[:2]}_{stamp}.png"),
        full_page=True,
    )
    return BookingAttemptResult(success, venue, slot, target_date, message, code)


def tomorrow_iso() -> str:
    return (datetime.now().astimezone() + timedelta(days=1)).date().isoformat()
