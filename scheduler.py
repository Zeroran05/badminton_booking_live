from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def next_daily_run(now: datetime, daily_at: str, run_now: bool = False) -> datetime:
    if run_now:
        return now
    hour, minute, second = (int(part) for part in daily_at.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


async def wait_until(moment: datetime, logger: logging.Logger, label: str) -> None:
    announced = False
    while True:
        seconds = (moment - datetime.now(moment.tzinfo)).total_seconds()
        if seconds <= 0:
            return
        if not announced:
            logger.info("Waiting until %s: %s", label, moment.isoformat())
            announced = True
        await asyncio.sleep(min(seconds, 30.0))


def schedule_times(
    config: dict,
    run_now: bool,
    release_delay_seconds: float = 0,
) -> tuple[datetime, datetime]:
    cfg = config["scheduler"]
    timezone = ZoneInfo(cfg.get("timezone", "Asia/Shanghai"))
    now = datetime.now(timezone)
    release = next_daily_run(now, cfg.get("daily_at", "08:00:00"), run_now)
    preopen = release - timedelta(seconds=int(cfg.get("browser_preopen_seconds", 120)))
    booking_start = release if run_now else release + timedelta(seconds=release_delay_seconds)
    return preopen, booking_start
