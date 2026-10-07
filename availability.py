from __future__ import annotations

import re
from datetime import date
from typing import Any

from models import Availability, SlotStatus


TIME_RE = re.compile(r"(?<!\d)(0?\d|1\d|2[0-3])(?:[:：点时](?:00)?)?\s*[-—~至]\s*(0?\d|1\d|2[0-3])(?:[:：点时](?:00)?)?(?!\d)")


def normalize_slot(text: str) -> str | None:
    match = TIME_RE.search(text.replace("－", "-").replace("–", "-"))
    if not match:
        return None
    start, end = (int(match.group(1)), int(match.group(2)))
    if not (0 <= start < end <= 24):
        return None
    return f"{start:02d}:00-{end:02d}:00"


def classify_slot(node: dict[str, Any]) -> SlotStatus:
    attrs = {str(k).lower(): str(v).lower() for k, v in node.get("attrs", {}).items()}
    classes = attrs.get("class", "")
    text = str(node.get("text", "")).lower()
    checked = node.get("checked") is True or attrs.get("aria-checked") == "true"
    disabled = (
        node.get("disabled") is True
        or "disabled" in attrs
        or attrs.get("aria-disabled") == "true"
        or any(word in classes for word in ("disabled", "is-disabled"))
    )
    if checked or any(word in classes for word in ("selected", "active", "checked")):
        return SlotStatus.SELECTED
    if disabled:
        return SlotStatus.DISABLED
    if any(word in text for word in ("已约", "约满", "不可选", "无余量", "占用")):
        return SlotStatus.UNAVAILABLE
    if any(word in classes for word in ("unavailable", "occupied", "booked", "full")):
        return SlotStatus.UNAVAILABLE
    if any(word in text for word in ("可预约", "可选", "有余量", "空闲")):
        return SlotStatus.AVAILABLE
    if node.get("interactive") and not disabled:
        return SlotStatus.AVAILABLE
    return SlotStatus.UNKNOWN


def build_availability(
    venue: int, target_date: date, nodes: list[dict[str, Any]]
) -> Availability:
    expected = [f"{hour:02d}:00-{hour + 1:02d}:00" for hour in range(8, 22)]
    slots = {slot: SlotStatus.UNKNOWN for slot in expected}
    for node in nodes:
        slot = normalize_slot(str(node.get("text", "")))
        if slot in slots:
            status = classify_slot(node)
            if slots[slot] == SlotStatus.UNKNOWN or status != SlotStatus.UNKNOWN:
                slots[slot] = status
    return Availability(
        venue=venue,
        venue_name=f"{venue}号羽毛球场",
        date=target_date.isoformat(),
        slots=slots,
    )

