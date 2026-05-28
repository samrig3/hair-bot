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
    to_date = date.today() + timedelta(days=LOOKAHEAD_DAYS)

    params = {
        "fromDate": from_date.isoformat(),
        "toDate": to_date.isoformat(),
        "resIds": STYLIST_RESOURCE_ID,
        "srvIds": service_id,
    }

    resp = requests.get(SLOTS_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    slots: List[Slot] = []
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
    return slots


def filter_by_scope(slots: List[Slot], broadened: bool) -> List[Slot]:
    """Keep slots whose tier is allowed for the current scope, sorted by
    tier (most preferred first), then by soonest date."""
    allowed = BROADENED_TIERS if broadened else PREFERRED_TIERS
    kept = [s for s in slots if s.tier in allowed]
    kept.sort(key=lambda s: (s.tier, s.start))
    return kept
