"""Persistent state management. State is stored in state.json and committed
back to the repo by GitHub Actions on each run.

State model:
- Multiple parallel "watches" can be active at once.
- Each watch has a unique ID, service, earliest_date, source ("cycle" or "ad_hoc"),
  and its own seen_slots dict.
- At most one cycle watch can exist (new one replaces old).
- Any number of ad-hoc watches can run in parallel.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

STATE_FILE = Path(__file__).parent / "state.json"

DEFAULT_STATE = {
    "watches": [],                  # list of watch dicts (see below)
    "run_counter": 0,
    "pending_telegram_offset": 0,
    "last_appointment": None,       # {"date": "...", "service": "...", "category": "..."}
    "last_autostart_kvitto": None,  # ISO date of the last kvitto we auto-started from
    "roots_since_highlights": 0,    # count of roots appointments since last highlights
}

# Each watch is:
# {
#   "id": "uuid",
#   "source": "cycle" or "ad_hoc",
#   "category": "roots_cut",
#   "earliest_date": "2026-07-22",      # ISO date
#   "created_at": "2026-05-27T14:00:00+00:00",
#   "seen_slots": {                      # slot_fingerprint -> ISO timestamp of last seen
#     "2026-07-26T11:00:00|roots_cut": "2026-05-27T14:32:00+00:00"
#   }
# }


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {**DEFAULT_STATE, "watches": []}
    with open(STATE_FILE, "r") as f:
        state = json.load(f)
    for k, v in DEFAULT_STATE.items():
        state.setdefault(k, v if not isinstance(v, list) else [])
    return state


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True, default=str)


def new_watch(source: str, category: str, earliest_date: str) -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "source": source,
        "category": category,
        "earliest_date": earliest_date,
        "broadened": False,          # False = preferred tiers only; True = all tiers
        "created_at": datetime.now(timezone.utc).isoformat(),
        "seen_slots": {},
        "notified_empty_preferred": False,  # have we sent the "no preferred slots" nudge?
        "last_heartbeat": None,             # ISO timestamp of last weekly status message
    }


def find_cycle_watch(state: dict) -> Optional[dict]:
    for w in state["watches"]:
        if w["source"] == "cycle":
            return w
    return None


def add_or_replace_cycle_watch(state: dict, watch: dict) -> None:
    """Cycle watches are unique — adding a new one removes any existing cycle watch."""
    state["watches"] = [w for w in state["watches"] if w["source"] != "cycle"]
    state["watches"].append(watch)


def add_adhoc_watch(state: dict, watch: dict) -> None:
    """Ad-hoc watches accumulate; multiple of the same category just overwrite."""
    state["watches"] = [
        w for w in state["watches"]
        if not (w["source"] == "ad_hoc" and w["category"] == watch["category"])
    ]
    state["watches"].append(watch)


def remove_watch(state: dict, watch_id: str) -> bool:
    before = len(state["watches"])
    state["watches"] = [w for w in state["watches"] if w["id"] != watch_id]
    return len(state["watches"]) < before


def remove_watch_by_category(state: dict, category: str) -> bool:
    """Remove first watch matching a category. Used when bot detects a booking."""
    for i, w in enumerate(state["watches"]):
        if w["category"] == category:
            del state["watches"][i]
            return True
    return False
