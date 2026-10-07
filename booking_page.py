from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from playwright.async_api import Frame, Locator, Page

from availability import build_availability
from models import Availability, BookingError, BookingStatus, SlotStatus
from portal import describe_locator


async def _visible(locator: Locator) -> Locator | None:
    for index in range(await locator.count()):
        item = locator.nth(index)
        if await item.is_visible():
            return item
    return None


async def _find_by_text(page: Page, text: str, exact: bool = True) -> tuple[Frame, Locator] | None:
    for frame in page.frames:
        for candidate in (
            frame.get_by_role("button", name=text, exact=exact),
            frame.get_by_role("radio", name=text, exact=exact),
            frame.get_by_role("checkbox", name=text, exact=exact),
            frame.get_by_text(text, exact=exact),
        ):
            item = await _visible(candidate)
            if item:
                return frame, item
    return None


async def save_failure_artifacts(page: Page, root: Path, logger: logging.Logger) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (root / "screenshots").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    try:
        await page.screenshot(path=str(root / "screenshots" / f"error_{stamp}.png"), full_page=True)
    except Exception as exc:
        logger.warning("Could not save error screenshot: %s", exc)
    for index, frame in enumerate(page.frames):
        try:
            html = await frame.content()
            suffix = "" if index == 0 else f"_frame_{index}"
            (root / "logs" / f"debug_page_{stamp}{suffix}.html").write_text(html, encoding="utf-8")
        except Exception as exc:
            logger.warning("Could not save frame %d HTML: %s", index, exc)


async def log_page_structure(page: Page, logger: logging.Logger) -> None:
    logger.info("Current URL: %s", page.url)
    logger.info("Page title: %s", await page.title())
    logger.info("iframe count: %d", max(0, len(page.frames) - 1))
    for index, frame in enumerate(page.frames):
        logger.info("Frame[%d] URL: %s", index, frame.url)


async def install_dom_submit_guard(page: Page, logger: logging.Logger) -> None:
    script = """
    (() => {
      const blocked = /^(提交|暂存)$/;
      const protect = () => {
        for (const el of document.querySelectorAll('button,input[type=submit],input[type=button],a,[role=button]')) {
          const label = (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, '');
          if (blocked.test(label)) {
            el.setAttribute('disabled', 'disabled');
            el.setAttribute('aria-disabled', 'true');
            el.style.pointerEvents = 'none';
            el.dataset.dryRunBlocked = 'true';
          }
        }
      };
      protect();
      new MutationObserver(protect).observe(document.documentElement, {subtree:true, childList:true});
    })();
    """
    await page.context.add_init_script(script=script)
    for frame in page.frames:
        try:
            await frame.evaluate(script)
        except Exception:
            pass
    logger.info("Dry-run DOM guard enabled for 提交/暂存")


async def accept_notice(page: Page, logger: logging.Logger) -> dict[str, str]:
    notice_text = "我已仔细阅读并同意以上预约须知"
    found = await _find_by_text(page, notice_text, exact=False)
    if not found:
        raise BookingError(BookingStatus.NOTICE_DIALOG_ERROR, "Notice checkbox/label not found")
    _, target = found
    checkbox = target
    if await target.get_attribute("role") != "checkbox" and await target.get_attribute("type") != "checkbox":
        nested = target.locator("input[type=checkbox]")
        if await nested.count():
            checkbox = nested.first
    checkbox_desc = await describe_locator(checkbox)
    logger.info("Notice dialog detected")
    if await checkbox.get_attribute("type") == "checkbox":
        await checkbox.check()
    else:
        await target.click()
    logger.info("Notice checkbox selected")

    confirm_found = None
    for frame in page.frames:
        candidate = frame.locator("button").filter(has_text=re.compile(r"确\s*认"))
        item = await _visible(candidate)
        if item:
            confirm_found = (frame, item)
            break
    if not confirm_found:
        raise BookingError(BookingStatus.NOTICE_DIALOG_ERROR, "Notice confirm button not found")
    _, confirm = confirm_found
    confirm_desc = await describe_locator(confirm)
    await confirm.wait_for(state="visible")
    for _ in range(20):
        if await confirm.is_enabled():
            break
        await page.wait_for_timeout(250)
    if not await confirm.is_enabled():
        raise BookingError(BookingStatus.NOTICE_DIALOG_ERROR, "Notice confirm remained disabled")
    await confirm.click()
    try:
        await target.wait_for(state="hidden", timeout=8_000)
    except Exception as exc:
        raise BookingError(BookingStatus.NOTICE_DIALOG_ERROR, "Notice did not close") from exc
    logger.info("Notice confirmed")
    return {"checkbox": checkbox_desc, "confirm": confirm_desc}


async def accept_notice_if_present(
    page: Page,
    logger: logging.Logger,
    timeout_ms: int = 5_000,
) -> bool:
    """Wait until either the notice or the usable booking form is rendered."""
    deadline = time.monotonic() + timeout_ms / 1_000
    notice_text = "我已仔细阅读并同意以上预约须知"
    while time.monotonic() < deadline:
        if await _find_by_text(page, notice_text, exact=False):
            await accept_notice(page, logger)
            return True

        if await _find_by_text(page, "联系方式", exact=True):
            # The form and modal are mounted by separate Vue updates. Give the
            # notice one short chance to appear before treating it as absent.
            await page.wait_for_timeout(100)
            if await _find_by_text(page, notice_text, exact=False):
                await accept_notice(page, logger)
                return True
            logger.info("Booking form ready; notice dialog was not shown")
            return False

        await page.wait_for_timeout(20)

    raise BookingError(
        BookingStatus.PAGE_STRUCTURE_CHANGED,
        "Neither the booking notice nor the contact form appeared",
    )


async def fill_contact(page: Page, phone: str, logger: logging.Logger) -> str:
    for frame in page.frames:
        candidates = [
            frame.get_by_label("联系方式", exact=False),
            frame.locator("input[name*=phone i], input[name*=mobile i], input[id*=phone i], input[id*=mobile i]"),
        ]
        label = frame.get_by_text("联系方式", exact=True)
        if await label.count():
            candidates.append(label.first.locator("xpath=following::input[1]"))
        for candidate in candidates:
            item = await _visible(candidate)
            if item:
                await item.fill(phone)
                if await item.input_value() != phone:
                    continue
                desc = await describe_locator(item)
                logger.info("Contact filled")
                logger.info("Contact locator: %s", desc)
                return desc
    raise BookingError(BookingStatus.CONTACT_INPUT_NOT_FOUND)


async def select_semantic_option(
    page: Page, text: str, error: BookingStatus, logger: logging.Logger
) -> str:
    found = await _find_by_text(page, text, exact=True)
    if not found:
        raise BookingError(error)
    _, target = found
    await target.click()
    await page.wait_for_timeout(50)
    state = await target.evaluate(
        """el => {
          const root = el.closest('label,[role=radio],[role=checkbox],[role=option],button') || el;
          const input = root.matches('input') ? root : root.querySelector('input');
          const ancestry = [root, root.parentElement, root.parentElement?.parentElement].filter(Boolean);
          return {
            checked: input ? input.checked : null,
            disabled: input ? input.disabled : null,
            ariaChecked: ancestry.map(x => x.getAttribute('aria-checked')),
            ariaSelected: ancestry.map(x => x.getAttribute('aria-selected')),
            class: ancestry.map(x => x.className || '').join(' '),
            outerHTML: root.outerHTML.slice(0, 1000)
          };
        }"""
    )
    selected = (
        state["checked"] is True
        or "true" in state["ariaChecked"]
        or "true" in state["ariaSelected"]
        or any(word in str(state["class"]).lower() for word in ("active", "selected", "checked"))
    )
    if not selected:
        raise BookingError(error, f"{text} click did not produce a selected DOM state: {state}")
    desc = await describe_locator(target)
    logger.info("Selected %s; state=%s", text, json.dumps(state, ensure_ascii=False))
    return desc


async def collect_known_dom(page: Page) -> dict[str, Any]:
    """Capture actual, sanitized DOM snippets for the acceptance report."""
    result: dict[str, Any] = {"venues": {}, "dates": {}, "time_nodes": []}
    for label in [*(f"{number}号羽毛球场" for number in range(1, 7)), "今天", "明天", "个人"]:
        found = await _find_by_text(page, label, exact=False)
        if not found:
            description = None
        else:
            _, locator = found
            description = await describe_locator(locator)
        if "羽毛球场" in label:
            result["venues"][label] = description
        elif label in {"今天", "明天"}:
            result["dates"][label] = description
        else:
            result["category"] = description
    _, time_nodes = await read_time_nodes(page)
    result["time_nodes"] = time_nodes
    return result


async def select_tomorrow(page: Page, logger: logging.Logger) -> tuple[str, str]:
    found = await _find_by_text(page, "明天", exact=False)
    if not found:
        raise BookingError(BookingStatus.TARGET_DATE_NOT_AVAILABLE)
    _, target = found
    semantic_state = await target.evaluate(
        """el => {
          const root = el.closest('label,[role=radio],button') || el;
          return {
            text: (root.innerText || root.textContent || '').trim().replace(/\s+/g, ' '),
            class: root.className || ''
          };
        }"""
    )
    text = semantic_state["text"]
    disabled = not await target.is_enabled() or await target.get_attribute("aria-disabled") == "true"
    classes = str(semantic_state["class"]).lower()
    desc = await describe_locator(target)
    if "约满" in text:
        logger.warning(BookingStatus.TARGET_DATE_FULL.value)
    if disabled or "disabled" in classes:
        if "约满" in text:
            return desc, BookingStatus.TARGET_DATE_FULL.value
        logger.warning(BookingStatus.TARGET_DATE_NOT_AVAILABLE.value)
        return desc, BookingStatus.TARGET_DATE_NOT_AVAILABLE.value
    await target.click()
    await page.wait_for_timeout(1_000)
    selected = await target.is_checked() if await target.get_attribute("type") == "radio" else False
    if not selected:
        selected = await target.get_attribute("aria-checked") == "true"
    if not selected:
        raise BookingError(BookingStatus.TARGET_DATE_NOT_AVAILABLE, "Tomorrow click did not select the date")
    logger.info("Target date: 明天")
    return desc, (BookingStatus.TARGET_DATE_FULL.value if "约满" in text else BookingStatus.SUCCESS.value)


async def read_time_nodes(page: Page) -> tuple[list[dict[str, Any]], list[str]]:
    nodes: list[dict[str, Any]] = []
    descriptions: list[str] = []
    script = """
    () => Array.from(document.querySelectorAll('input,button,label,[role=button],[role=radio],[role=checkbox],li,div,span'))
      .filter(el => {
        const text = (el.innerText || el.value || el.textContent || '').trim();
        const hasTime = /(?:0?\\d|1\\d|2[0-3])(?:[:：点时](?:00)?)?\\s*[-—~至－–]\\s*(?:0?\\d|1\\d|2[0-3])/.test(text);
        const childHasTime = Array.from(el.children).some(c => /(?:0?\\d|1\\d|2[0-3])(?:[:：点时](?:00)?)?\\s*[-—~至－–]\\s*(?:0?\\d|1\\d|2[0-3])/.test((c.innerText || c.value || c.textContent || '').trim()));
        return hasTime && (!childHasTime || ['INPUT','BUTTON','LABEL'].includes(el.tagName));
      })
      .map(el => ({
        text: (el.innerText || el.value || el.textContent || '').trim().replace(/\\s+/g,' '),
        tag: el.tagName.toLowerCase(),
        attrs: Object.fromEntries(Array.from(el.attributes).map(a => [a.name, a.value])),
        checked: 'checked' in el ? el.checked : null,
        disabled: 'disabled' in el ? el.disabled : null,
        interactive: ['INPUT','BUTTON','LABEL'].includes(el.tagName) || ['button','radio','checkbox'].includes(el.getAttribute('role')),
        outerHTML: el.outerHTML.slice(0, 1200)
      }))
    """
    for frame_index, frame in enumerate(page.frames):
        try:
            frame_nodes = await frame.evaluate(script)
            for node in frame_nodes:
                node["frame_index"] = frame_index
                descriptions.append(node["outerHTML"])
            nodes.extend(frame_nodes)
        except Exception:
            continue
    return nodes, descriptions


async def read_availability(
    page: Page, venue: int, logger: logging.Logger, date_status: str = BookingStatus.SUCCESS.value
) -> tuple[Availability, list[str]]:
    logger.info("Reading time slots")
    nodes, descriptions = await read_time_nodes(page)
    if not nodes:
        raise BookingError(BookingStatus.TIME_SLOTS_NOT_FOUND)
    target_date = (datetime.now().astimezone() + timedelta(days=1)).date()
    result = build_availability(venue, target_date, nodes)
    if date_status != BookingStatus.SUCCESS.value:
        # Time controls remain visually enabled even when no date can be selected.
        # Their state is not tomorrow's availability and must not be reported as such.
        result.slots = {slot: SlotStatus.UNKNOWN for slot in result.slots}
        logger.warning("Time controls are not bound to tomorrow; reporting UNKNOWN")
    for slot, status in result.slots.items():
        logger.info("%s  %s", slot, status.value)
    return result, descriptions


def submission_blocked(*_: Any, **__: Any) -> None:
    raise RuntimeError("Booking submission blocked in dry-run mode")
