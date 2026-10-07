from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from auth import ensure_login, ensure_service_login
from booking_page import (
    accept_notice_if_present,
    fill_contact,
    install_dom_submit_guard,
    save_failure_artifacts,
    select_semantic_option,
)
from browser import create_browser, install_dry_run_network_guard
from live_booking import (
    is_release_not_open_message,
    scan_all_venues,
    select_booking_choice,
    submit_selected_booking,
    tomorrow_iso,
)
from logger import setup_logger
from models import BookingError, BookingPlan, BookingStatus
from portal import open_booking_page
from scheduler import schedule_times, wait_until
from strategy import choose_continuation_plan, choose_plan


ROOT = Path(__file__).resolve().parent
LIVE_CONFIRMATION = "I_UNDERSTAND_THIS_CREATES_REAL_BOOKINGS"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HITsz badminton booking scheduler")
    parser.add_argument("--live", action="store_true", help="Allow real booking submission")
    parser.add_argument("--run-now", action="store_true", help="Run immediately instead of next 08:00")
    parser.add_argument(
        "--release-delay-seconds",
        type=float,
        default=0,
        help="Wait this many seconds after the scheduled release before opening booking",
    )
    parser.add_argument("--once", action="store_true", help="Scan once instead of retrying")
    parser.add_argument("--only-venue", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--only-slot", help=argparse.SUPPRESS)
    return parser.parse_args()


def load_config() -> dict[str, Any]:
    with (ROOT / "config.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def live_authorized(args: argparse.Namespace, config: dict[str, Any]) -> bool:
    if not args.live:
        return False
    if config["booking"].get("live_enabled") is not True:
        raise BookingError(
            BookingStatus.LIVE_MODE_NOT_AUTHORIZED,
            "Set booking.live_enabled: true before using --live",
        )
    if os.getenv("LIVE_BOOKING_CONFIRM") != LIVE_CONFIRMATION:
        raise BookingError(
            BookingStatus.LIVE_MODE_NOT_AUTHORIZED,
            f"Set LIVE_BOOKING_CONFIRM={LIVE_CONFIRMATION} in .env",
        )
    return True


async def prepare_portal_page(context, config, logger, username: str, password: str):
    open_pages = [candidate for candidate in context.pages if not candidate.is_closed()]
    page = open_pages[0] if open_pages else await context.new_page()
    await ensure_login(page, config, logger, username, password)
    return page


async def open_booking_service(context, page, config, logger, username: str, password: str):
    result = await open_booking_page(context, page, config["portal"]["service_name"], logger)
    page = result.page
    await ensure_service_login(page, config, logger, username, password)
    await accept_notice_if_present(page, logger)
    return page


async def return_to_booking_form(page, logger) -> bool:
    """Return from the progress page without a portal round trip when possible."""
    if page.is_closed():
        return False
    try:
        tab = page.get_by_text("羽毛球场地预约", exact=True).first
        if not await tab.count() or not await tab.is_visible():
            return False
        await tab.click()
        await accept_notice_if_present(page, logger)
        logger.info("Returned directly from progress query to booking form")
        return True
    except Exception as exc:
        logger.warning("Direct return to booking form failed; using portal fallback: %s", exc)
        return False


async def run(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    logger = setup_logger(ROOT / "logs")
    config = load_config()
    context = None
    page = None

    try:
        phone = os.getenv("CONTACT_PHONE", "").strip()
        if not phone:
            raise BookingError(BookingStatus.CONTACT_PHONE_MISSING)
        username = os.getenv("PORTAL_USERNAME", "").strip()
        password = os.getenv("PORTAL_PASSWORD", "")
        is_live = live_authorized(args, config)
        if (args.only_venue is None) != (args.only_slot is None):
            raise BookingError(
                BookingStatus.SUBMISSION_FAILED,
                "--only-venue and --only-slot must be provided together",
            )
        if args.only_venue is not None and not is_live:
            raise BookingError(
                BookingStatus.LIVE_MODE_NOT_AUTHORIZED,
                "An explicit selection is only available in live mode",
            )
        if args.release_delay_seconds < 0:
            raise BookingError(
                BookingStatus.SUBMISSION_FAILED,
                "--release-delay-seconds must be zero or greater",
            )
        preopen_at, target_at = schedule_times(
            config,
            args.run_now,
            args.release_delay_seconds,
        )
        await wait_until(preopen_at, logger, "browser pre-open")

        async with async_playwright() as playwright:
            context = await create_browser(playwright, config, ROOT, logger)
            portal_page = await prepare_portal_page(
                context, config, logger, username, password
            )
            await wait_until(target_at, logger, "booking release")
            page = await open_booking_service(
                context, portal_page, config, logger, username, password
            )
            if is_live:
                logger.warning("LIVE MODE ARMED: successful selections will create real bookings")
            else:
                await install_dry_run_network_guard(context, config, logger)
                await install_dom_submit_guard(page, logger)
                logger.info("DRY RUN: submission is blocked")

            await fill_contact(page, phone, logger)
            await select_semantic_option(page, "个人", BookingStatus.CATEGORY_NOT_FOUND, logger)

            booking_cfg = config["booking"]
            retry_cfg = config["scheduler"]
            venue_priority = [int(item) for item in booking_cfg["venue_priority"]]
            effective_max = int(booking_cfg.get("max_successful_bookings", 2))
            target_date = tomorrow_iso()
            deadline = datetime.now().astimezone() + timedelta(
                seconds=int(retry_cfg.get("retry_window_seconds", 120))
            )
            completed: set[tuple[int, str]] = set()
            submission_attempts = 0
            release_gate_rejections = 0
            continuation_slots: list[str] = []
            continuation_preferred_venue: int | None = None
            cycle = 0

            while datetime.now().astimezone() <= deadline:
                cycle += 1
                logger.info("Availability scan cycle %d", cycle)
                if args.only_venue is not None:
                    records = []
                    plan = BookingPlan(
                        target_date,
                        [(args.only_venue, args.only_slot)],
                        "explicit_selection",
                    )
                else:
                    ordered_groups = sorted(
                        booking_cfg["time_groups"], key=lambda item: item["priority"]
                    )
                    allow_different = bool(
                        booking_cfg.get("allow_different_venues_for_consecutive_hours", True)
                    )
                    early_stop_slots = (
                        continuation_slots
                        if continuation_slots
                        else list(ordered_groups[0]["slots"])
                    )
                    records = await scan_all_venues(
                        page,
                        venue_priority,
                        target_date,
                        logger,
                        completed,
                        early_stop_slots=early_stop_slots,
                        allow_different_venues=allow_different,
                    )
                    if continuation_slots:
                        plan = choose_continuation_plan(
                            records,
                            continuation_slots,
                            venue_priority,
                            continuation_preferred_venue,
                        )
                        if plan is None:
                            logger.info(
                                "No venue can complete the adjacent hour; falling back to other windows"
                            )
                            continuation_slots = []
                            continuation_preferred_venue = None
                    else:
                        plan = None
                    if plan is None:
                        plan = choose_plan(
                            records,
                            booking_cfg["time_groups"],
                            venue_priority,
                            allow_different,
                        )
                snapshot = {
                    "captured_at": datetime.now().astimezone().isoformat(),
                    "target_date": target_date,
                    "plan": None if plan is None else {
                        "reason": plan.reason,
                        "selections": plan.selections,
                    },
                    "availability": [record.to_dict() for record in records],
                }
                (ROOT / "logs" / "latest_scan.json").write_text(
                    json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
                )

                if plan is None:
                    logger.info("No preferred slot is currently available")
                elif not is_live:
                    logger.info("DRY RUN selected plan: %s", plan)
                    await page.screenshot(
                        path=str(ROOT / "screenshots" / "dry_run_plan.png"), full_page=True
                    )
                    return 0
                else:
                    logger.info("Attempting plan: %s", plan)
                    release_gate_blocked = False
                    for selection_index, (venue, slot) in enumerate(plan.selections):
                        if len(completed) >= effective_max:
                            break
                        if submission_attempts >= int(booking_cfg.get("max_submission_attempts", 6)):
                            break
                        if page.is_closed():
                            logger.info("Booking page closed after submission; reopening it")
                            portal_page = await prepare_portal_page(
                                context, config, logger, username, password
                            )
                            page = await open_booking_service(
                                context, portal_page, config, logger, username, password
                            )
                        await fill_contact(page, phone, logger)
                        await select_semantic_option(
                            page, "个人", BookingStatus.CATEGORY_NOT_FOUND, logger
                        )
                        try:
                            await select_booking_choice(page, venue, target_date, slot)
                            result = await submit_selected_booking(
                                page, venue, target_date, slot, ROOT, logger
                            )
                        except BookingError as exc:
                            logger.warning("Selection changed before submit: %s", exc)
                            break
                        if not result.success and is_release_not_open_message(result.message):
                            release_gate_rejections += 1
                            release_gate_blocked = True
                            logger.warning(
                                "RELEASE_NOT_OPEN: server gate is still closed; "
                                "retrying without consuming the booking attempt limit "
                                "(gate rejection %d)",
                                release_gate_rejections,
                            )
                            break
                        submission_attempts += 1
                        if result.success:
                            completed.add((venue, slot))
                            continuation_slots = [
                                pending_slot
                                for pending_slot in continuation_slots
                                if pending_slot != slot
                            ]
                            logger.info("BOOKING_SUCCESS venue=%d slot=%s", venue, slot)
                            if args.only_venue is not None:
                                return 0
                            if (
                                selection_index < len(plan.selections) - 1
                                and len(completed) < effective_max
                            ):
                                continuation_slots = [
                                    pending_slot
                                    for _, pending_slot in plan.selections[selection_index + 1 :]
                                    if not any(
                                        completed_slot == pending_slot
                                        for _, completed_slot in completed
                                    )
                                ]
                                continuation_preferred_venue = venue
                                logger.info("First booking succeeded; returning for the next hour")
                                if not await return_to_booking_form(page, logger):
                                    portal_page = await prepare_portal_page(
                                        context, config, logger, username, password
                                    )
                                    page = await open_booking_service(
                                        context, portal_page, config, logger, username, password
                                    )
                        else:
                            logger.warning(
                                "BOOKING_REJECTED venue=%d slot=%s message=%s",
                                venue,
                                slot,
                                result.message,
                            )
                            break

                    if release_gate_blocked:
                        if args.once:
                            logger.info("Single-scan mode finished")
                            break
                        await asyncio.sleep(
                            float(retry_cfg.get("release_retry_interval_seconds", 0.5))
                        )
                        continue

                    if continuation_slots and len(completed) < effective_max:
                        logger.info(
                            "Retrying the adjacent hour immediately across venue priority: %s",
                            continuation_slots,
                        )
                        continue

                if len(completed) >= effective_max:
                    logger.info("Booking target achieved: %s", sorted(completed))
                    return 0
                if args.once:
                    logger.info("Single-scan mode finished")
                    break
                if submission_attempts >= int(booking_cfg.get("max_submission_attempts", 6)):
                    logger.error("Submission attempt limit reached")
                    break
                await asyncio.sleep(float(retry_cfg.get("retry_interval_seconds", 3)))

            if completed:
                logger.warning("PARTIAL_SUCCESS: %s", sorted(completed))
                return 4
            logger.error("No booking was obtained within the retry window")
            return 5
    except BookingError as exc:
        logger.error("%s: %s", exc.status.value, exc)
        if page is not None:
            await save_failure_artifacts(page, ROOT, logger)
        return 2
    except Exception as exc:
        logger.exception("%s: %s", BookingStatus.UNKNOWN_ERROR.value, exc)
        if page is not None:
            await save_failure_artifacts(page, ROOT, logger)
        return 3
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass


if __name__ == "__main__":
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11+ is required")
    raise SystemExit(asyncio.run(run(parse_args())))
