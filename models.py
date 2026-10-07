from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class SlotStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    SELECTED = "SELECTED"
    UNAVAILABLE = "UNAVAILABLE"
    DISABLED = "DISABLED"
    UNKNOWN = "UNKNOWN"


class BookingStatus(StrEnum):
    SUCCESS = "SUCCESS"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    PORTAL_ENTRY_NOT_FOUND = "PORTAL_ENTRY_NOT_FOUND"
    NOTICE_DIALOG_ERROR = "NOTICE_DIALOG_ERROR"
    CONTACT_PHONE_MISSING = "CONTACT_PHONE_MISSING"
    CONTACT_INPUT_NOT_FOUND = "CONTACT_INPUT_NOT_FOUND"
    CATEGORY_NOT_FOUND = "CATEGORY_NOT_FOUND"
    VENUE_NOT_FOUND = "VENUE_NOT_FOUND"
    TARGET_DATE_FULL = "TARGET_DATE_FULL"
    TARGET_DATE_NOT_AVAILABLE = "TARGET_DATE_NOT_AVAILABLE"
    TIME_SLOTS_NOT_FOUND = "TIME_SLOTS_NOT_FOUND"
    PAGE_STRUCTURE_CHANGED = "PAGE_STRUCTURE_CHANGED"
    NETWORK_ERROR = "NETWORK_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"
    LIVE_MODE_NOT_AUTHORIZED = "LIVE_MODE_NOT_AUTHORIZED"
    SUBMISSION_FAILED = "SUBMISSION_FAILED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"


class BookingError(RuntimeError):
    def __init__(self, status: BookingStatus, message: str = "") -> None:
        self.status = status
        super().__init__(message or status.value)


@dataclass(slots=True)
class Availability:
    venue: int
    venue_name: str
    date: str
    slots: dict[str, SlotStatus] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BookingPlan:
    date: str
    selections: list[tuple[int, str]] = field(default_factory=list)
    reason: str = ""


@dataclass(slots=True)
class BookingAttemptResult:
    success: bool
    venue: int
    slot: str
    date: str
    message: str
    response_code: int | None = None
