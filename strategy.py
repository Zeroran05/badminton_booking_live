from __future__ import annotations

from collections.abc import Iterable

from models import Availability, BookingPlan, SlotStatus


def choose_continuation_plan(
    records: Iterable[Availability],
    slots: list[str],
    venue_priority: list[int],
    preferred_venue: int | None = None,
) -> BookingPlan | None:
    """Finish an already-started time window before considering new windows."""
    records = list(records)
    if not records or not slots:
        return None
    by_venue = {item.venue: item for item in records}
    ordered_venues = list(venue_priority)
    if preferred_venue in ordered_venues:
        ordered_venues.remove(preferred_venue)
        ordered_venues.insert(0, preferred_venue)
    selectable = {SlotStatus.AVAILABLE, SlotStatus.SELECTED}
    for slot in slots:
        for venue in ordered_venues:
            item = by_venue.get(venue)
            if item and item.slots.get(slot) in selectable:
                reason = "continuation:same_venue" if venue == preferred_venue else "continuation:other_venue"
                return BookingPlan(item.date, [(venue, slot)], reason)
    return None


def choose_plan(
    records: Iterable[Availability],
    time_groups: list[dict],
    venue_priority: list[int],
    allow_different_venues: bool = True,
) -> BookingPlan | None:
    """Pure planning function for a future release; it never performs a booking."""
    records = list(records)
    if not records:
        return None
    by_venue = {item.venue: item for item in records}
    groups = sorted(time_groups, key=lambda item: item["priority"])
    selectable = {SlotStatus.AVAILABLE, SlotStatus.SELECTED}

    # First exhaust every acceptable two-hour window.  This makes a complete
    # consecutive pair beat a single hour in a higher-priority time window.
    for group in groups:
        slots = list(group["slots"])
        for venue in venue_priority:
            item = by_venue.get(venue)
            if item and all(item.slots.get(slot) in selectable for slot in slots):
                return BookingPlan(item.date, [(venue, slot) for slot in slots], f"{group['name']}:same_venue")
        if allow_different_venues and all(
            any(by_venue.get(v) and by_venue[v].slots.get(slot) in selectable for v in venue_priority)
            for slot in slots
        ):
            selections = []
            for slot in slots:
                venue = next(v for v in venue_priority if by_venue.get(v) and by_venue[v].slots.get(slot) in selectable)
                selections.append((venue, slot))
            return BookingPlan(records[0].date, selections, f"{group['name']}:different_venues")

    # Only fall back to one hour when no configured window can supply a
    # continuous two-hour booking.
    for group in groups:
        slots = list(group["slots"])
        for slot in slots:
            for venue in venue_priority:
                if by_venue.get(venue) and by_venue[venue].slots.get(slot) in selectable:
                    return BookingPlan(records[0].date, [(venue, slot)], f"{group['name']}:single_hour")
    return None
