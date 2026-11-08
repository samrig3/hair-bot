"""One-off slot check, run on demand via the "Check Now" workflow.

Prints Josefine's open slots for the chosen services and days. Read-only:
it never books anything, never touches state.json, never messages Telegram.

Usage: python check_now.py <service[,service...]> <days>
  service: a key from config.SERVICE_IDS, e.g. roots_cut, haircut
  days:    "sat", "fri,sat", or "all"
"""

import sys
from datetime import date

from cliento import get_available_slots
from config import SERVICE_IDS

DAY_KEYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def main() -> None:
    services = [s.strip() for s in (sys.argv[1] if len(sys.argv) > 1 else "roots_cut").split(",") if s.strip()]
    days_arg = (sys.argv[2] if len(sys.argv) > 2 else "sat").lower()
    weekdays = set(range(7)) if days_arg == "all" else {DAY_KEYS[d.strip()[:3]] for d in days_arg.split(",")}

    for service in services:
        if service not in SERVICE_IDS:
            print(f"### {service}: unknown service. Valid: {', '.join(SERVICE_IDS)}")
            continue
        slots = sorted(
            (s for s in get_available_slots(service, date.today()) if s.start.weekday() in weekdays),
            key=lambda s: s.start,
        )
        print(f"### {service}: {len(slots)} slot(s) ({days_arg})")
        for s in slots:
            print(f"- {s.display()}")
        print()


if __name__ == "__main__":
    main()
