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
    is_transient_selection_conflict_message,
    scan_all_venues,
    select_booking_choice,
    submit_selected_booking,
    tomorrow_iso,
)
from logger import setup_logger
from models import BookingAttemptResult, BookingError, BookingPlan, BookingStatus
from portal import open_booking_page
from scheduler import schedule_times, wait_until
from strategy import choose_continuation_plan, choose_plan


ROOT = Path(__file__).resolve().parent
LIVE_CONFIRMATION = "I_UNDERSTAND_THIS_CREATES_REAL_BOOKINGS"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HITsz badminton booking scheduler")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Configuration file path (relative paths are resolved from the project directory)",
    )
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


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    with config_path.open(encoding="utf-8") as handle:
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
    portal_cfg = config["portal"]
    result = await open_booking_page(
        context,
        page,
        portal_cfg["service_name"],
        logger,
        float(portal_cfg.get("service_open_wait_seconds", 15)),
    )
    page = result.page
    try:
        await ensure_service_login(page, config, logger, username, password)
        await accept_notice_if_present(
            page,
            logger,
            timeout_ms=int(float(portal_cfg.get("service_render_wait_seconds", 15)) * 1_000),
        )
    except Exception:
        await save_failure_artifacts(page, ROOT, logger)
        raise
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


async def reset_booking_form_after_rejection(
    page, config, logger, username: str, password: str
) -> bool:
    """Reload the service form so a rejected radio selection cannot leak forward."""
    if page.is_closed():
        return False
    try:
        await page.reload(wait_until="domcontentloaded")
        await ensure_service_login(page, config, logger, username, password)
        await accept_notice_if_present(page, logger)
        logger.info("Booking form reset after rejected submission")
        return True
    except Exception as exc:
        logger.warning("Booking form reset failed; using portal fallback: %s", exc)
        return False


def booking_record_matches(row_text: str, venue: int, target_date: str, slot: str) -> bool:
    """Match one progress-query row without depending on table column layout."""
    normalized = "".join(row_text.split()).replace("—", "-").replace("–", "-")
    start_hour, end_hour = (int(part.split(":", 1)[0]) for part in slot.split("-", 1))
    slot_tokens = {
        f"{start_hour}点-{end_hour}点",
        f"{start_hour}点--{end_hour}点",
    }
    return (
        target_date in normalized
        and f"{venue}号羽毛球场" in normalized
        and any(token in normalized for token in slot_tokens)
        and "成功预约" in normalized
    )


def is_ambiguous_submission_timeout(result: BookingAttemptResult) -> bool:
    """A response-less submit needs reconciliation and a fresh form, not a fatal exit."""
    return result.response_code is None and any(
        marker in result.message
        for marker in (
            "Submission response timed out",
            "Submission request was not sent",
        )
    )


async def confirm_booking_in_progress(
    page, venue: int, target_date: str, slot: str, logger
) -> bool:
    """Resolve an ambiguous submit timeout from the service progress table."""
    if page.is_closed():
        return False
    try:
        progress_tab = page.get_by_text("进度查询", exact=True).first
        if await progress_tab.count() and await progress_tab.is_visible():
            await progress_tab.click()
            await page.wait_for_timeout(350)
        rows = await page.locator("tbody tr").all_inner_texts()
        confirmed = any(
            booking_record_matches(row, venue, target_date, slot) for row in rows
        )
        logger.info(
            "Parallel timeout reconciliation venue=%d slot=%s confirmed=%s",
            venue,
            slot,
            confirmed,
        )
        return confirmed
    except Exception as exc:
        logger.warning(
            "Could not reconcile parallel timeout venue=%d slot=%s: %s",
            venue,
            slot,
            exc,
        )
        return False


def should_parallelize_plan(
    plan: BookingPlan,
    completed: set[tuple[int, str]],
    effective_max: int,
    enabled: bool,
) -> bool:
    """Use two isolated forms only for an untouched same-venue two-hour pair."""
    return (
        enabled
        and not completed
        and effective_max >= 2
        and len(plan.selections) == 2
        and plan.selections[0][0] == plan.selections[1][0]
        and "same_venue" in plan.reason
    )


async def open_parallel_booking_form(
    context,
    source_page,
    config,
    logger,
    username: str,
    password: str,
):
    """Open an independent form at the already-authenticated service URL."""
    parallel_page = await context.new_page()
    portal_cfg = config["portal"]
    timeout_ms = int(float(portal_cfg.get("service_render_wait_seconds", 15)) * 1_000)
    try:
        await parallel_page.goto(
            source_page.url,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        await ensure_service_login(
            parallel_page, config, logger, username, password
        )
        await accept_notice_if_present(parallel_page, logger, timeout_ms=timeout_ms)
        logger.info("Parallel booking form ready")
        return parallel_page
    except Exception:
        await save_failure_artifacts(parallel_page, ROOT, logger)
        try:
            await parallel_page.close()
        except Exception:
            pass
        raise


async def prepare_booking_selection(
    page,
    phone: str,
    venue: int,
    target_date: str,
    slot: str,
    logger,
) -> None:
    await fill_contact(page, phone, logger)
    await select_semantic_option(page, "个人", BookingStatus.CATEGORY_NOT_FOUND, logger)
    await select_booking_choice(page, venue, target_date, slot)


async def submit_until_release_opens(
    page,
    venue: int,
    target_date: str,
    slot: str,
    deadline: datetime,
    retry_interval_seconds: float,
    logger,
    lane: int,
    response_timeout_ms: int = 2_000,
):
    """Submit one prepared form until the server gate opens or rejects it normally."""
    gate_rejections = 0
    result = None
    probe_index = 0
    while datetime.now().astimezone() <= deadline:
        result = await submit_selected_booking(
            page,
            venue,
            target_date,
            slot,
            ROOT,
            logger,
            capture_before=probe_index == 0,
            capture_gate_rejection=probe_index == 0,
            response_timeout_ms=response_timeout_ms,
            # A successful lane is settled independently; the other lane must
            # not wait for its progress-page navigation before submitting.
            success_wait_ms=0,
        )
        probe_index += 1
        if result.success or not is_release_not_open_message(result.message):
            break
        gate_rejections += 1
        logger.warning(
            "PARALLEL_RELEASE_NOT_OPEN lane=%d venue=%d slot=%s rejection=%d",
            lane,
            venue,
            slot,
            gate_rejections,
        )
        await asyncio.sleep(retry_interval_seconds)
    return result, gate_rejections


async def attempt_parallel_pair(
    context,
    primary_page,
    plan: BookingPlan,
    target_date: str,
    phone: str,
    config,
    logger,
    username: str,
    password: str,
    deadline: datetime,
    retry_interval_seconds: float,
):
    """Pre-arm two pages and let both probe the release gate independently."""
    secondary_page = None
    try:
        secondary_page = await open_parallel_booking_form(
            context,
            primary_page,
            config,
            logger,
            username,
            password,
        )
        pages = [primary_page, secondary_page]
        await asyncio.gather(
            *(
                prepare_booking_selection(
                    worker_page,
                    phone,
                    venue,
                    target_date,
                    slot,
                    logger,
                )
                for worker_page, (venue, slot) in zip(pages, plan.selections)
            )
        )
    except Exception as exc:
        logger.warning("Parallel pair preparation failed; using sequential fallback: %s", exc)
        if secondary_page is not None and not secondary_page.is_closed():
            try:
                await secondary_page.close()
            except Exception:
                pass
        return None

    # Both pages are equal release probes. If the service leaves either request
    # pending, return_exceptions keeps the healthy lane running and the caller
    # reconciles the timed-out lane before switching to sequential fallback.
    logger.info("Parallel pair armed: %s", plan.selections)
    logger.info("Both parallel lanes are probing the release gate independently")
    raw_submissions = await asyncio.gather(
        *(
            submit_until_release_opens(
                worker_page,
                venue,
                target_date,
                slot,
                deadline,
                retry_interval_seconds,
                logger,
                lane,
                response_timeout_ms=2_000,
            )
            for lane, (worker_page, (venue, slot)) in enumerate(
                zip(pages, plan.selections), start=1
            )
        ),
        return_exceptions=True,
    )
    submissions = []
    for worker_page, (venue, slot), raw in zip(
        pages, plan.selections, raw_submissions
    ):
        if not isinstance(raw, BaseException):
            submissions.append(raw)
            continue
        if isinstance(raw, asyncio.CancelledError):
            raise raw
        logger.warning(
            "PARALLEL_LANE_ERROR venue=%d slot=%s error=%s",
            venue,
            slot,
            raw,
        )
        confirmed = await confirm_booking_in_progress(
            worker_page, venue, target_date, slot, logger
        )
        if confirmed:
            submissions.append(
                (
                    BookingAttemptResult(
                        True,
                        venue,
                        slot,
                        target_date,
                        "success confirmed from progress query",
                        0,
                    ),
                    0,
                )
            )
        else:
            submissions.append(
                (
                    BookingAttemptResult(
                        False,
                        venue,
                        slot,
                        target_date,
                        f"{type(raw).__name__}: {raw}",
                        None,
                    ),
                    0,
                )
            )
    return [
        (worker_page, venue, slot, result, gate_rejections)
        for worker_page, (venue, slot), (result, gate_rejections) in zip(
            pages, plan.selections, submissions
        )
    ]


async def run(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    logger = setup_logger(ROOT / "logs")
    config = load_config(args.config)
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
            failed_submission_attempts = 0
            max_failed_submission_attempts = int(
                booking_cfg.get(
                    "max_failed_submission_attempts",
                    booking_cfg.get("max_submission_attempts", 6),
                )
            )
            release_gate_rejections = 0
            continuation_slots: list[str] = []
            continuation_preferred_venue: int | None = None
            serial_fallback_active = False
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
                    parallel_results = None
                    parallel_attempted = should_parallelize_plan(
                        plan,
                        completed,
                        effective_max,
                        bool(booking_cfg.get("parallel_consecutive_bookings", False))
                        and not serial_fallback_active,
                    )
                    if parallel_attempted:
                        parallel_results = await attempt_parallel_pair(
                            context,
                            page,
                            plan,
                            target_date,
                            phone,
                            config,
                            logger,
                            username,
                            password,
                            deadline,
                            float(retry_cfg.get("release_retry_interval_seconds", 0.1)),
                        )
                        if parallel_results is None:
                            serial_fallback_active = True
                            logger.warning(
                                "SERIAL_FALLBACK_ACTIVE: parallel preparation failed; "
                                "all remaining attempts will use the sequential strategy"
                            )

                    if parallel_results is not None:
                        reusable_page = None
                        pending_slots: list[str] = []
                        preferred_venue = plan.selections[0][0]
                        parallel_degraded = False
                        for worker_page, venue, slot, result, gate_rejections in parallel_results:
                            release_gate_rejections += gate_rejections
                            if result is None or (
                                not result.success
                                and is_release_not_open_message(result.message)
                            ):
                                release_gate_blocked = True
                                pending_slots.append(slot)
                                continue
                            if result.success:
                                completed.add((venue, slot))
                                logger.info(
                                    "PARALLEL_BOOKING_SUCCESS venue=%d slot=%s",
                                    venue,
                                    slot,
                                )
                                continue

                            pending_slots.append(slot)
                            parallel_degraded = True
                            ambiguous_timeout = is_ambiguous_submission_timeout(result)
                            transient_selection_conflict = (
                                is_transient_selection_conflict_message(result.message)
                            )
                            if ambiguous_timeout:
                                logger.warning(
                                    "PARALLEL_TIMEOUT_UNCONFIRMED: retrying without consuming "
                                    "the failed-submission limit"
                                )
                            elif transient_selection_conflict:
                                logger.warning(
                                    "TRANSIENT_SELECTION_CONFLICT: parallel form residue; "
                                    "resetting without consuming the failure limit"
                                )
                            else:
                                failed_submission_attempts += 1
                            logger.warning(
                                "PARALLEL_BOOKING_REJECTED venue=%d slot=%s message=%s "
                                "failed_attempts=%d/%d",
                                venue,
                                slot,
                                result.message,
                                failed_submission_attempts,
                                max_failed_submission_attempts,
                            )
                            if ambiguous_timeout:
                                logger.warning(
                                    "Discarding timed-out parallel page before retrying "
                                    "venue=%d slot=%s",
                                    venue,
                                    slot,
                                )
                                if not worker_page.is_closed():
                                    try:
                                        await worker_page.close()
                                    except Exception:
                                        pass
                            elif await reset_booking_form_after_rejection(
                                worker_page, config, logger, username, password
                            ):
                                reusable_page = worker_page

                        if parallel_degraded:
                            serial_fallback_active = True
                            logger.warning(
                                "SERIAL_FALLBACK_ACTIVE: parallel submission was incomplete; "
                                "all remaining attempts will use the sequential strategy"
                            )

                        if len(completed) >= effective_max:
                            logger.info("Booking target achieved in parallel: %s", sorted(completed))
                            return 0

                        if reusable_page is None and not release_gate_blocked:
                            portal_page = await prepare_portal_page(
                                context, config, logger, username, password
                            )
                            reusable_page = await open_booking_service(
                                context, portal_page, config, logger, username, password
                            )
                        if reusable_page is not None:
                            page = reusable_page

                        for worker_page, *_ in parallel_results:
                            if worker_page is page or worker_page.is_closed():
                                continue
                            try:
                                await worker_page.close()
                            except Exception:
                                pass

                        if release_gate_blocked:
                            if args.once:
                                logger.info("Single-scan mode finished")
                                break
                            await asyncio.sleep(
                                float(retry_cfg.get("release_retry_interval_seconds", 0.1))
                            )
                            continue

                        if completed and pending_slots:
                            continuation_slots = pending_slots
                            continuation_preferred_venue = preferred_venue
                            logger.info(
                                "Parallel pair partially succeeded; retrying adjacent hour "
                                "sequentially: %s",
                                continuation_slots,
                            )
                            continue

                        if failed_submission_attempts >= max_failed_submission_attempts:
                            logger.error("Failed submission attempt limit reached")
                            break
                        await asyncio.sleep(float(retry_cfg.get("retry_interval_seconds", 0.1)))
                        continue

                    for selection_index, (venue, slot) in enumerate(plan.selections):
                        if len(completed) >= effective_max:
                            break
                        if failed_submission_attempts >= max_failed_submission_attempts:
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
                        except BookingError as exc:
                            logger.warning("Selection changed before submit: %s", exc)
                            break

                        # Keep the exact same choice selected while the release gate is
                        # closed. Re-scanning and changing venues here leaves the previous
                        # Vue radio state behind and can trigger "此时间已被选中".
                        result = None
                        probe_index = 0
                        while datetime.now().astimezone() <= deadline:
                            try:
                                result = await submit_selected_booking(
                                    page,
                                    venue,
                                    target_date,
                                    slot,
                                    ROOT,
                                    logger,
                                    capture_before=probe_index == 0,
                                    capture_gate_rejection=probe_index == 0,
                                    # Once one booking is already secured, a silent
                                    # follow-up must recover quickly rather than block
                                    # the remaining retry window for 20 seconds.
                                    response_timeout_ms=2_000,
                                )
                            except BookingError as exc:
                                if exc.status is not BookingStatus.NETWORK_ERROR:
                                    raise
                                confirmed = await confirm_booking_in_progress(
                                    page, venue, target_date, slot, logger
                                )
                                result = BookingAttemptResult(
                                    confirmed,
                                    venue,
                                    slot,
                                    target_date,
                                    (
                                        "success confirmed from progress query"
                                        if confirmed
                                        else str(exc)
                                    ),
                                    0 if confirmed else None,
                                )
                                break
                            probe_index += 1
                            if result.success or not is_release_not_open_message(result.message):
                                break
                            release_gate_rejections += 1
                            logger.warning(
                                "RELEASE_NOT_OPEN: server gate is still closed; "
                                "retrying the same selection without consuming the failure limit "
                                "(gate rejection %d)",
                                release_gate_rejections,
                            )
                            if args.once:
                                release_gate_blocked = True
                                break
                            await asyncio.sleep(
                                float(retry_cfg.get("release_retry_interval_seconds", 0.1))
                            )

                        if result is None:
                            break
                        if not result.success and is_release_not_open_message(result.message):
                            release_gate_blocked = True
                            break
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
                            ambiguous_timeout = is_ambiguous_submission_timeout(result)
                            transient_selection_conflict = (
                                is_transient_selection_conflict_message(result.message)
                            )
                            if ambiguous_timeout:
                                logger.warning(
                                    "SUBMISSION_TIMEOUT_UNCONFIRMED: retrying without consuming "
                                    "the failed-submission limit"
                                )
                            elif transient_selection_conflict:
                                logger.warning(
                                    "TRANSIENT_SELECTION_CONFLICT: stale form selection detected; "
                                    "resetting without consuming the failure limit"
                                )
                            else:
                                failed_submission_attempts += 1
                            logger.warning(
                                "BOOKING_REJECTED venue=%d slot=%s message=%s "
                                "failed_attempts=%d/%d",
                                venue,
                                slot,
                                result.message,
                                failed_submission_attempts,
                                max_failed_submission_attempts,
                            )
                            if ambiguous_timeout:
                                logger.warning(
                                    "Discarding timed-out booking page before retrying "
                                    "venue=%d slot=%s",
                                    venue,
                                    slot,
                                )
                                if not page.is_closed():
                                    try:
                                        await page.close()
                                    except Exception:
                                        pass
                                portal_page = await prepare_portal_page(
                                    context, config, logger, username, password
                                )
                                page = await open_booking_service(
                                    context, portal_page, config, logger, username, password
                                )
                            elif not await reset_booking_form_after_rejection(
                                page, config, logger, username, password
                            ):
                                portal_page = await prepare_portal_page(
                                    context, config, logger, username, password
                                )
                                page = await open_booking_service(
                                    context, portal_page, config, logger, username, password
                                )
                            break

                    if release_gate_blocked:
                        if args.once:
                            logger.info("Single-scan mode finished")
                            break
                        await asyncio.sleep(
                            float(retry_cfg.get("release_retry_interval_seconds", 0.1))
                        )
                        continue

                    if continuation_slots and len(completed) < effective_max:
                        if failed_submission_attempts >= max_failed_submission_attempts:
                            logger.error("Failed submission attempt limit reached")
                            break
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
                if failed_submission_attempts >= max_failed_submission_attempts:
                    logger.error("Failed submission attempt limit reached")
                    break
                await asyncio.sleep(float(retry_cfg.get("retry_interval_seconds", 0.1)))

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
