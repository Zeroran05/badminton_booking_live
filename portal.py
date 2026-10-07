from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from playwright.async_api import BrowserContext, Frame, Locator, Page

from models import BookingError, BookingStatus


@dataclass(slots=True)
class EntryResult:
    page: Page
    mode: str
    locator: str


async def describe_locator(locator: Locator) -> str:
    return await locator.evaluate(
        """el => {
          const keep = ['id','name','type','role','value','href','data-id','data-value'];
          const attrs = keep.map(k => el.hasAttribute(k) ? `${k}=${JSON.stringify(el.getAttribute(k))}` : '')
                            .filter(Boolean).join(' ');
          const text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 120);
          return `<${el.tagName.toLowerCase()} ${attrs}> ${JSON.stringify(text)}`;
        }"""
    )


async def _find_entry(page: Page, text: str) -> tuple[Frame, Locator] | None:
    for frame in page.frames:
        candidates = [
            frame.get_by_role("link", name=text, exact=True),
            frame.get_by_role("button", name=text, exact=True),
            frame.get_by_text(text, exact=True),
        ]
        for candidate in candidates:
            if await candidate.count():
                item = candidate.first
                if await item.is_visible():
                    return frame, item
    return None


async def open_booking_page(
    context: BrowserContext, page: Page, service_name: str, logger: logging.Logger
) -> EntryResult:
    logger.info("Searching for badminton booking service")
    found = await _find_entry(page, service_name)
    if not found:
        raise BookingError(BookingStatus.PORTAL_ENTRY_NOT_FOUND)
    frame, entry = found
    description = await describe_locator(entry)
    logger.info("Portal entry locator: %s", description)
    previous_pages = list(context.pages)
    previous_url = page.url
    logger.info("Opening badminton booking")
    await entry.click()
    # New tabs normally appear immediately. Poll briefly instead of paying a
    # fixed 2.5-second delay during the release rush.
    for _ in range(50):
        if any(candidate not in previous_pages for candidate in context.pages):
            break
        if page.url != previous_url:
            break
        await asyncio.sleep(0.02)
    new_pages = [p for p in context.pages if p not in previous_pages]
    if new_pages:
        target = new_pages[-1]
        await target.wait_for_load_state("domcontentloaded", timeout=60_000)
        mode = "new_tab"
    else:
        target = page
        mode = "spa_or_current_page" if page.url == previous_url else "current_page_navigation"
    if frame != page.main_frame:
        mode = "iframe_entry_then_" + mode
    logger.info("Booking entry mode: %s", mode)
    return EntryResult(target, mode, description)
