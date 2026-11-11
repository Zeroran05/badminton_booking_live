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


async def _looks_like_booking_service(page: Page) -> bool:
    """Recognize a same-page/SPA service only after its real form is visible."""
    for frame in page.frames:
        if await frame.get_by_text("联系方式", exact=True).count():
            return True
        if await frame.get_by_text(
            "我已仔细阅读并同意以上预约须知", exact=False
        ).count():
            return True
    return False


async def _wait_for_booking_target(
    context: BrowserContext,
    page: Page,
    previous_pages: list[Page],
    previous_url: str,
    timeout_seconds: float,
    poll_interval_seconds: float = 0.05,
) -> tuple[Page, str]:
    """Wait for a delayed popup/navigation without mistaking the portal for the service."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        new_pages = [candidate for candidate in context.pages if candidate not in previous_pages]
        if new_pages:
            target = new_pages[-1]
            remaining_ms = max(
                1_000,
                int((deadline - asyncio.get_running_loop().time()) * 1_000),
            )
            await target.wait_for_load_state("domcontentloaded", timeout=remaining_ms)
            return target, "new_tab"
        if page.url != previous_url:
            remaining_ms = max(
                1_000,
                int((deadline - asyncio.get_running_loop().time()) * 1_000),
            )
            await page.wait_for_load_state("domcontentloaded", timeout=remaining_ms)
            return page, "current_page_navigation"
        if await _looks_like_booking_service(page):
            return page, "spa_or_current_page"
        await asyncio.sleep(poll_interval_seconds)
    raise BookingError(
        BookingStatus.PAGE_STRUCTURE_CHANGED,
        f"Booking service did not open within {timeout_seconds:g} seconds",
    )


async def open_booking_page(
    context: BrowserContext,
    page: Page,
    service_name: str,
    logger: logging.Logger,
    open_timeout_seconds: float = 15,
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
    target, mode = await _wait_for_booking_target(
        context,
        page,
        previous_pages,
        previous_url,
        open_timeout_seconds,
    )
    if frame != page.main_frame:
        mode = "iframe_entry_then_" + mode
    logger.info("Booking entry mode: %s", mode)
    return EntryResult(target, mode, description)
