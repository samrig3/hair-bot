"""Scrapes Cliento for available appointment slots at Urban Hair (Stockholm).

API discovered via browser network inspection:
  GET https://cliento.com/api/v2/partner/cliento/{SALON_ID}/resources/slots
      ?fromDate=YYYY-MM-DD&toDate=YYYY-MM-DD&resIds={STYLIST_ID}&srvIds={SERVICE_ID}

No auth required beyond the x-clientowidgetversion header.
"""

from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import List

import requests

from config import (
    STYLIST_RESOURCE_ID, SERVICE_IDS, slot_tier,
    PREFERRED_TIERS, BROADENED_TIERS, SALON_CLOSING_HOUR,
)

SALON_ID = "4eZp3ZfJAdTwvVLk0oyqmj"
SLOTS_URL = f"https://cliento.com/api/v2/partner/cliento/{SALON_ID}/resources/slots"

HEADERS = {
    "accept": "application/json",
    "x-clientowidgetversion": "v3-microsite",
    "referer": "https://cliento.com/business/urban-hair-ab-urbanhair/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
}

LOOKAHEAD_DAYS = 180

# Cliento rejects long date ranges (HTTP 400). Fetch availability in chunks of
# this many days and combine. The booking widget itself requests short ranges;
# 14 days is a safe chunk size. (If you still see 400s in the logs, lower this.)
CHUNK_DAYS = 14

# Stop fetching further chunks after this many consecutive empty ones — i.e.
# once we've gone past the end of Josefine's published calendar. Avoids ~13
# pointless requests per scrape when the calendar only goes a couple months out.
EMPTY_CHUNKS_BEFORE_STOP = 3


@dataclass
class Slot:
    start: datetime
    duration_minutes: int
    category: str

    @property
    def fingerprint(self) -> str:
        return f"{self.start.isoformat()}|{self.category}"

    @property
    def tier(self) -> int:
        return slot_tier(self.start.weekday(), self.start.hour, self.start.minute)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.duration_minutes)

    @property
    def runs_past_closing(self) -> bool:
        closing = SALON_CLOSING_HOUR.get(self.start.weekday())
        if closing is None:
            return False
        end = self.end
        return (end.hour + end.minute / 60) > closing

    def display(self) -> str:
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        day = day_names[self.start.weekday()]
        dur = f"{self.duration_minutes} min" if self.duration_minutes else ""
        base = f"{day} {self.start.strftime('%b %d, %H:%M')}"
        if dur:
            base += f" ({dur})"
        return base


def get_available_slots(category: str, earliest_date: date) -> List[Slot]:
    """Fetch all bookable slots for the given category from earliest_date onward."""
    service_id = SERVICE_IDS.get(category)
    if not service_id:
        print(f"No service ID configured for category '{category}', skipping.")
        return []

    from_date = max(earliest_date, date.today())
    end_date = date.today() + timedelta(days=LOOKAHEAD_DAYS)

    # Cliento rejects very long date ranges (the widget only ever requests a
    # week or two at a time). Fetch in chunks and combine. Stop early once we
    # hit consecutive empty chunks (past the end of the published calendar).
    slots: List[Slot] = []
    chunk_start = from_date
    empty_chunks = 0
    while chunk_start <= end_date:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), end_date)
        params = {
            "fromDate": chunk_start.isoformat(),
            "toDate": chunk_end.isoformat(),
            "resIds": STYLIST_RESOURCE_ID,
            "srvIds": service_id,
        }
        try:
            resp = requests.get(SLOTS_URL, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"Chunk {chunk_start}..{chunk_end} failed for {category}: {e}")
            chunk_start = chunk_end + timedelta(days=1)
            continue

        chunk_slot_count = 0
        for resource in data.get("resourceSlots", []):
            for s in resource.get("slots", []):
                if s.get("notAvailable", False):
                    continue
                if s.get("bookedSlots", 0) >= s.get("maxSlots", 1):
                    continue
                try:
                    start = datetime.strptime(
                        f"{s['date']} {s['time']}", "%Y-%m-%d %H:%M:%S"
                    )
                except (KeyError, ValueError):
                    continue
                if start.date() < earliest_date:
                    continue
                slots.append(Slot(
                    start=start,
                    duration_minutes=s.get("length", 0),
                    category=category,
                ))
                chunk_slot_count += 1

        # Track empty chunks so we can stop once past the published calendar.
        if chunk_slot_count == 0:
            empty_chunks += 1
            if empty_chunks >= EMPTY_CHUNKS_BEFORE_STOP:
                break
        else:
            empty_chunks = 0

        chunk_start = chunk_end + timedelta(days=1)

    return slots


def filter_by_scope(slots: List[Slot], broadened: bool) -> List[Slot]:
    """Keep slots whose tier is allowed for the current scope, sorted by
    tier (most preferred first), then by soonest date."""
    allowed = BROADENED_TIERS if broadened else PREFERRED_TIERS
    kept = [s for s in slots if s.tier in allowed]
    kept.sort(key=lambda s: (s.tier, s.start))
    return kept
