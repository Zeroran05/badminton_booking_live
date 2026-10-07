from __future__ import annotations

import logging
import time
from typing import Any

from playwright.async_api import Page

from models import BookingError, BookingStatus


LOGIN_MARKERS = ("login", "auth", "cas", "sso")


async def autofill_login_form(
    page: Page,
    username: str,
    password: str,
    auto_submit: bool,
    logger: logging.Logger,
) -> bool:
    """Fill the normal SSO form without logging or persisting either credential."""
    if not username or not password:
        logger.info("Portal credentials not configured; waiting for manual login")
        return False

    username_input = page.locator("#username")
    password_input = page.locator("#password")
    if not await username_input.count() or not await password_input.count():
        logger.warning("Login form not recognized; waiting for manual login")
        return False

    await username_input.wait_for(state="visible", timeout=10_000)
    await password_input.wait_for(state="visible", timeout=10_000)

    # The current SSO page marks these fields readonly to suppress browser autofill.
    # Removing that presentation attribute does not bypass authentication; the page's
    # normal login button still performs its own encryption and server-side checks.
    await username_input.evaluate("el => el.removeAttribute('readonly')")
    await password_input.evaluate("el => el.removeAttribute('readonly')")
    await username_input.fill(username)
    await password_input.fill(password)
    if await username_input.input_value() != username or await password_input.input_value() != password:
        raise BookingError(BookingStatus.PAGE_STRUCTURE_CHANGED, "Credential form fill did not persist")
    logger.info("Portal username and password filled from local .env")

    captcha_container = page.locator("#captchaDiv")
    captcha_input = page.locator("#captcha")
    captcha_required = (
        await captcha_container.count() > 0
        and await captcha_container.is_visible()
        and await captcha_input.count() > 0
    )
    if captcha_required:
        logger.warning("Manual CAPTCHA verification required; credentials remain filled")
        return True

    if auto_submit:
        submit = page.locator("#login_submit")
        if not await submit.count() or not await submit.is_visible():
            raise BookingError(BookingStatus.PAGE_STRUCTURE_CHANGED, "Login button not found")
        await submit.click()
        logger.info("Normal portal login button clicked")
    else:
        logger.info("Credentials filled; click the login button manually")
    return True


async def _looks_logged_in(page: Page, service_name: str) -> bool:
    try:
        if await page.get_by_text(service_name, exact=True).count():
            return True
        if await page.locator("input[type=password]").count():
            return False
        url = page.url.lower()
        return bool(url) and not any(marker in url for marker in LOGIN_MARKERS)
    except Exception:
        return False


async def ensure_login(
    page: Page,
    config: dict[str, Any],
    logger: logging.Logger,
    username: str = "",
    password: str = "",
) -> None:
    portal = config["portal"]
    logger.info("Opening HITsz portal")
    await page.goto(portal["url"], wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(2_000)
    if await _looks_logged_in(page, portal["service_name"]):
        logger.info("Login detected")
        logger.info("Portal home loaded")
        return

    logger.warning(BookingStatus.LOGIN_REQUIRED.value)
    auth_config = config.get("auth", {})
    if auth_config.get("auto_fill_credentials", True):
        await autofill_login_form(
            page,
            username,
            password,
            bool(auth_config.get("auto_submit_login", True)),
            logger,
        )
    logger.info("Please complete login manually in Edge")
    logger.info("Waiting for portal login")
    deadline = time.monotonic() + int(portal.get("login_wait_seconds", 600))
    while time.monotonic() < deadline:
        await page.wait_for_timeout(1_000)
        if await _looks_logged_in(page, portal["service_name"]):
            logger.info("Login detected")
            logger.info("Portal home loaded")
            return
    raise BookingError(BookingStatus.LOGIN_REQUIRED, "Manual login timed out")


async def ensure_service_login(
    page: Page,
    config: dict[str, Any],
    logger: logging.Logger,
    username: str = "",
    password: str = "",
) -> None:
    """Handle a second CAS challenge opened by an individual portal service."""
    # open_booking_page already waits for DOMContentLoaded.  A short render
    # grace period is enough and avoids losing a full second after 08:00.
    await page.wait_for_timeout(100)
    login_form_present = await page.locator("input[type=password]").count() > 0
    if not login_form_present and not any(marker in page.url.lower() for marker in LOGIN_MARKERS):
        return

    logger.warning("Service requested unified authentication again")
    auth_config = config.get("auth", {})
    await autofill_login_form(
        page,
        username,
        password,
        bool(auth_config.get("auto_submit_login", True)),
        logger,
    )
    deadline = time.monotonic() + int(config["portal"].get("login_wait_seconds", 600))
    while time.monotonic() < deadline:
        await page.wait_for_timeout(1_000)
        login_form_present = await page.locator("input[type=password]").count() > 0
        if not login_form_present and not any(marker in page.url.lower() for marker in LOGIN_MARKERS):
            logger.info("Service login detected")
            return
    raise BookingError(BookingStatus.LOGIN_REQUIRED, "Service login timed out")
