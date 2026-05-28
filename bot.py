"""Main entry point. Runs every 10 minutes via GitHub Actions.

On each run:
1. Increment run counter.
2. Fetch new Telegram updates and handle messages / button presses.
3. If due: check Yahoo inbox for booking confirmations (auto-stops matching watches).
4. If due: scrape Cliento for each active watch and notify about new slots.
5. Save state.
"""

import sys
from datetime import datetime, date, timedelta, timezone

from config import (
    STYLIST_NAME,
    CYCLE_COOLDOWNS_WEEKS,
    CYCLE_SERVICES,
    SCRAPE_EVERY_N_RUNS,
    EMAIL_CHECK_EVERY_N_RUNS,
    SEEN_SLOT_TTL_HOURS,
    MAX_NOTIFICATIONS_PER_SCRAPE,
    HEARTBEAT_DAYS,
    CLOSEST_ALTERNATIVES_COUNT,
    ROOTS_BEFORE_HIGHLIGHTS,
    HIGHLIGHTS_DEFAULT_CATEGORY,
)
from state import (
    load_state, save_state, new_watch,
    add_or_replace_cycle_watch, add_adhoc_watch,
    find_cycle_watch, remove_watch, remove_watch_by_category,
)
from cliento import get_available_slots, filter_by_scope
from email_check import check_for_new_booking, check_for_completed_appointment
import telegram_bot as tg


# ---- Service category helpers ----

def category_display(cat: str) -> str:
    return {
        "highlights_uncolored_cut": "Highlights (uncolored) + cut",
        "highlights_colored_cut":   "Highlights (colored) + cut",
        "roots_cut":                "Roots + cut",
        "roots":                    "Roots only",
        "haircut":                  "Haircut",
        "haircut_treatment":        "Haircut + treatment",
        "wash_style":               "Wash & style",
    }.get(cat, cat.replace("_", " ").title())


# Cliento service name → internal category (for parsing confirmation emails)
SERVICE_NAME_TO_CATEGORY = {
    "ROOTS (FÄRG UTVÄXT) INKL KLIPP": "roots_cut",
    "ROOTS (FÄRG UTVÄXT)": "roots",
    "SLINGOR / UPPLJUSNING (PÅ OFÄRGAT HÅR) INKL KLIPP": "highlights_uncolored_cut",
    "SLINGOR / UPPLJUSNING (PÅ FÄRGAT HÅR) INKL KLIPP": "highlights_colored_cut",
    "HAIRCUT (KLIPPNING)": "haircut",
    "HAIRCUT (KLIPPNING) MED INTENSIVT VÅRDANDE BEHANDLING - K18 ELLER EPRES": "haircut_treatment",
    "TVÄTT OCH STYLE": "wash_style",
}


def infer_category(service_name: str) -> str:
    """Map a Cliento service name (from email) to our internal category."""
    upper = (service_name or "").upper()
    for name, cat in SERVICE_NAME_TO_CATEGORY.items():
        if name.upper() == upper:
            return cat
    if "SLINGOR" in upper or "UPPLJUS" in upper:
        if "OFÄRGAT" in upper or "OFARGAT" in upper:
            return "highlights_uncolored_cut"
        if "FÄRGAT" in upper or "FARGAT" in upper:
            return "highlights_colored_cut"
        return "highlights_uncolored_cut"
    if "ROOT" in upper or "UTVÄXT" in upper or "UTVAXT" in upper:
        return "roots_cut" if "KLIPP" in upper else "roots"
    if "KLIPPNING" in upper or "HAIRCUT" in upper:
        if "VÅRDANDE" in upper or "VARDANDE" in upper or "K18" in upper or "EPRES" in upper:
            return "haircut_treatment"
        return "haircut"
    if "TVÄTT" in upper or "TVATT" in upper or "STYLE" in upper:
        return "wash_style"
    return "other"


def suggest_next_cycle_category(last_category: str, roots_since_highlights: int) -> str:
    """Suggest the next cycle appointment based on what was just done and how many
    roots have been done since the last highlights.

    Pattern: highlights → roots → roots → highlights → ...
    After ROOTS_BEFORE_HIGHLIGHTS roots in a row, suggest highlights next.
    """
    if last_category in ("highlights_uncolored_cut", "highlights_colored_cut"):
        # Just had highlights → next is roots
        return "roots_cut"
    if last_category in ("roots_cut", "roots"):
        # Had roots — if we've now hit the threshold, switch to highlights
        if roots_since_highlights >= ROOTS_BEFORE_HIGHLIGHTS:
            return HIGHLIGHTS_DEFAULT_CATEGORY
        return "roots_cut"
    # Fallback for non-cycle last service
    return "roots_cut"


def update_roots_streak(state: dict, category: str) -> None:
    """Update the roots-since-highlights counter based on a completed appointment."""
    if category in ("highlights_uncolored_cut", "highlights_colored_cut"):
        state["roots_since_highlights"] = 0
    elif category in ("roots_cut", "roots"):
        state["roots_since_highlights"] = state.get("roots_since_highlights", 0) + 1
    # Other services (wash, haircut) don't affect the highlights/roots cycle count.


def service_buttons(prefix: str) -> list:
    """Inline keyboard for picking a service category."""
    return [
        [{"text": "Roots + cut",                  "callback_data": f"{prefix}:roots_cut"}],
        [{"text": "Highlights (uncolored) + cut", "callback_data": f"{prefix}:highlights_uncolored_cut"}],
        [{"text": "Highlights (colored) + cut",   "callback_data": f"{prefix}:highlights_colored_cut"}],
        [{"text": "Roots only",                   "callback_data": f"{prefix}:roots"}],
        [{"text": "Haircut",                      "callback_data": f"{prefix}:haircut"}],
        [{"text": "Haircut + treatment",          "callback_data": f"{prefix}:haircut_treatment"}],
        [{"text": "Wash & style",                 "callback_data": f"{prefix}:wash_style"}],
    ]


# ---- Telegram message routing ----

def handle_text_message(text: str, state: dict) -> None:
    t = text.strip().lower()

    if t in ("/start", "start", "hi", "hello"):
        send_welcome()
        return
    if "just had" in t or "had an appointment" in t:
        start_appointment_flow(state)
        return
    if "look for" in t or t.startswith("/look"):
        start_lookup_flow()
        return
    if t == "status":
        send_status(state)
        return
    if "stop" in t:
        offer_stop_options(state)
        return

    tg.send_message("I didn't understand that. Use the buttons at the bottom 👇")


def handle_callback(callback_data: str, state: dict) -> None:
    parts = callback_data.split(":", 1)
    action = parts[0]
    arg = parts[1] if len(parts) > 1 else ""

    if action == "confirm_cycle":
        # confirm_cycle:<category>
        start_cycle_search(state, arg)
    elif action == "change_cycle_service":
        ask_for_cycle_service()
    elif action == "set_cycle":
        start_cycle_search(state, arg)
    elif action == "lookup":
        # lookup:<category> — start ad-hoc search
        start_adhoc_search(state, arg)
    elif action == "keep_waiting":
        # keep_waiting:<watch_id> — user acknowledges empty preferred, stays narrow
        tg.send_message(
            "👍 Staying on Saturday + Friday afternoon only. "
            "I'll keep watching and let you know if that changes."
        )
    elif action == "broaden":
        # broaden:<watch_id>
        for w in state["watches"]:
            if w["id"] == arg:
                w["broadened"] = True
                w["seen_slots"] = {}  # reset so newly-eligible slots get notified
                tg.send_message(
                    f"🔍 Broadened <b>{category_display(w['category'])}</b> to all days. "
                    "I'll now show every day's slots, preferred ones (⭐) first."
                )
                break
    elif action == "narrow":
        # narrow:<watch_id>
        for w in state["watches"]:
            if w["id"] == arg:
                w["broadened"] = False
                w["seen_slots"] = {}
                tg.send_message(
                    f"🔙 Narrowed <b>{category_display(w['category'])}</b> back to "
                    "Saturday + Friday afternoon only."
                )
                break
    elif action == "booking_final":
        # booking_final:<watch_id>
        for w in state["watches"]:
            if w["id"] == arg:
                cat = w["category"]
                src = w["source"]
                remove_watch(state, arg)
                tail = ""
                if src == "cycle":
                    tail = "\n\nTap <b>Just had an appointment</b> after your visit to start the next cycle search."
                tg.send_message(
                    f"✅ Stopped the {category_display(cat)} search.{tail}"
                )
                break
        else:
            tg.send_message("That search is no longer active.")
    elif action == "booking_backup":
        # booking_backup:<watch_id>|<date time>
        wid, _, booking_str = arg.partition("|")
        bdate, _, btime = booking_str.partition(" ")
        for w in state["watches"]:
            if w["id"] == wid:
                w["backup"] = {"date": bdate, "time": btime}
                tg.send_message(
                    f"🔖 Noted — holding <b>{bdate} {btime}</b> as a backup. "
                    f"I'll keep looking for <b>{category_display(w['category'])}</b> and "
                    "remind you of the backup with each new slot. When you book a better "
                    "one, tell me to stop and cancel the backup yourself on Cliento."
                )
                break
        else:
            tg.send_message("That search is no longer active.")
    elif action == "stop_watch":
        # stop_watch:<watch_id>
        if remove_watch(state, arg):
            tg.send_message("Stopped that search ✅")
        send_status(state)
    elif action == "stop_all":
        n = len(state["watches"])
        state["watches"] = []
        tg.send_message(f"Stopped all {n} searches ✅")
    elif action == "skip_session":
        tg.send_message("OK, nothing started.")


# ---- Welcome / Status ----

def send_welcome() -> None:
    tg.send_message(
        f"Hi! I watch Cliento for available slots with <b>{STYLIST_NAME.title()}</b> "
        "at Urban Hair and ping you when Friday afternoon or Saturday slots open up.\n\n"
        "<b>Just had an appointment</b> — start a new cycle search\n"
        "<b>Look for a slot</b> — search for any specific service now\n"
        "<b>Status</b> — see what's being watched\n"
        "<b>Stop searching</b> — end a watch"
    )
    tg.set_main_menu()


def send_status(state: dict) -> None:
    if not state["watches"]:
        lines = ["<b>No active searches.</b>"]
        last = state.get("last_appointment")
        if last:
            lines.append(f"\nLast appointment: {last['date']} — {category_display(last.get('category', '?'))}")
        lines.append("\nTap <b>Just had an appointment</b> or <b>Look for a slot</b> to start.")
        tg.send_message("\n".join(lines))
        return

    # One message per watch, each with its own broaden/narrow + stop buttons
    header = f"<b>{len(state['watches'])} active search" + ("es" if len(state["watches"]) > 1 else "") + ":</b>"
    tg.send_message(header + "\n🔁 = cycle • ⚡ = ad-hoc • ⭐ = preferred times")

    for w in state["watches"]:
        tag = "🔁" if w["source"] == "cycle" else "⚡"
        scope = "all days" if w.get("broadened") else "Sat + Fri afternoon"
        backup = w.get("backup")
        backup_line = f"\nBackup held: {backup['date']} {backup['time']}" if backup else ""
        text = (
            f"{tag} <b>{category_display(w['category'])}</b>\n"
            f"From: {w['earliest_date']}\n"
            f"Scope: {scope}{backup_line}"
        )
        if w.get("broadened"):
            scope_btn = {"text": "🔙 Narrow to preferred", "callback_data": f"narrow:{w['id']}"}
        else:
            scope_btn = {"text": "🔍 Broaden to all days", "callback_data": f"broaden:{w['id']}"}
        buttons = [
            [scope_btn],
            [{"text": "Stop this search", "callback_data": f"stop_watch:{w['id']}"}],
        ]
        tg.send_message(text, buttons=buttons)


def offer_stop_options(state: dict) -> None:
    if not state["watches"]:
        tg.send_message("No active searches.")
        return
    buttons = []
    for w in state["watches"]:
        buttons.append([{
            "text": f"Stop: {category_display(w['category'])}",
            "callback_data": f"stop_watch:{w['id']}"
        }])
    if len(state["watches"]) > 1:
        buttons.append([{"text": "🛑 Stop all", "callback_data": "stop_all:_"}])
    tg.send_message("Which search to stop?", buttons=buttons)


# ---- "Just had an appointment" flow (cycle) ----

def start_appointment_flow(state: dict) -> None:
    tg.send_message("Checking for your latest receipt (kvitto)... 🔍")
    # Kvittos can lag a little; look back ~10 days for a completed appointment.
    since = datetime.now(timezone.utc) - timedelta(days=10)
    completed = check_for_completed_appointment(since)

    if completed is None:
        tg.send_message(
            "I couldn't find a recent receipt from Urban Hair. "
            "What service did you just have?",
            buttons=service_buttons(prefix="set_cycle"),
        )
        return

    appt_date, service_name = completed

    if service_name is None:
        # Found a kvitto but couldn't match it to a booking → ask what it was
        tg.send_message(
            f"I see you had an appointment on {appt_date.strftime('%b %d')} "
            f"(receipt found), but couldn't tell which service. What did you have?",
            buttons=service_buttons(prefix="set_cycle"),
        )
        state["last_appointment"] = {
            "date": appt_date.isoformat(),
            "service": None,
            "category": None,
        }
        return

    category = infer_category(service_name)
    state["last_appointment"] = {
        "date": appt_date.isoformat(),
        "service": service_name,
        "category": category,
    }

    if category not in CYCLE_SERVICES:
        tg.send_message(
            f"I see you had <b>{service_name}</b> on {appt_date.strftime('%b %d')}.\n\n"
            "That's not a cycle service. What should I set up as your next cycle search?",
            buttons=service_buttons(prefix="set_cycle"),
        )
        return

    # Update the roots/highlights streak (guard against double-counting the same
    # kvitto, which the auto-start path may also have processed).
    if state.get("last_autostart_kvitto") != appt_date.isoformat():
        update_roots_streak(state, category)
        state["last_autostart_kvitto"] = appt_date.isoformat()

    suggested = suggest_next_cycle_category(category, state.get("roots_since_highlights", 0))
    weeks = CYCLE_COOLDOWNS_WEEKS[suggested]
    streak_note = ""
    if suggested == HIGHLIGHTS_DEFAULT_CATEGORY:
        streak_note = (
            f"\n\n(You've had {state.get('roots_since_highlights', 0)} roots since "
            "your last highlights, so I think it's highlights time.)"
        )
    tg.send_message(
        f"I see you had <b>{service_name}</b> on {appt_date.strftime('%b %d')}.\n\n"
        f"Want me to search for <b>{category_display(suggested)}</b> starting in {weeks} weeks?"
        f"{streak_note}",
        buttons=[
            [{"text": f"✅ Yes, {category_display(suggested)}",
              "callback_data": f"confirm_cycle:{suggested}"}],
            [{"text": "Different service",
              "callback_data": "change_cycle_service:_"}],
            [{"text": "Skip for now",
              "callback_data": "skip_session:_"}],
        ],
    )


def ask_for_cycle_service() -> None:
    tg.send_message(
        "Which service should the cycle search look for?",
        buttons=service_buttons(prefix="set_cycle"),
    )


def start_cycle_search(state: dict, category: str) -> None:
    """Start (or replace) the cycle search.

    The cooldown is measured from the last appointment date (from the kvitto)
    when we have it, so 'next roots in 8 weeks' means 8 weeks after the actual
    appointment, not 8 weeks from when you happened to tap the button.
    """
    weeks = CYCLE_COOLDOWNS_WEEKS.get(category, 0)

    anchor = date.today()
    last = state.get("last_appointment")
    if last and last.get("date"):
        try:
            anchor = date.fromisoformat(last["date"])
        except ValueError:
            pass

    earliest = anchor + timedelta(weeks=weeks)
    # Never search in the past — if the cooldown window already elapsed, start today.
    if earliest < date.today():
        earliest = date.today()

    watch = new_watch(source="cycle", category=category, earliest_date=earliest.isoformat())
    add_or_replace_cycle_watch(state, watch)
    save_state(state)

    if weeks == 0:
        cooldown_msg = "starting today"
    else:
        cooldown_msg = f"starting {earliest.strftime('%b %d')} ({weeks} weeks after your last appointment)"

    tg.send_message(
        f"🔁 <b>Cycle search started</b>\n"
        f"Service: {category_display(category)}\n"
        f"Stylist: {STYLIST_NAME.title()}\n"
        f"Looking from: {cooldown_msg}\n"
        f"Times: Saturday + Friday afternoon (tap Status to broaden)\n\n"
        f"I'll ping you when matching slots appear."
    )


# ---- "Look for a slot" flow (ad-hoc) ----

def start_lookup_flow() -> None:
    tg.send_message(
        "Which service should I look for?",
        buttons=service_buttons(prefix="lookup"),
    )


def start_adhoc_search(state: dict, category: str) -> None:
    """Start an ad-hoc search starting today, regardless of cooldown."""
    earliest = date.today()
    watch = new_watch(source="ad_hoc", category=category, earliest_date=earliest.isoformat())
    add_adhoc_watch(state, watch)
    save_state(state)

    tg.send_message(
        f"⚡ <b>Ad-hoc search started</b>\n"
        f"Service: {category_display(category)}\n"
        f"Stylist: {STYLIST_NAME.title()}\n"
        f"Looking from: today\n"
        f"Times: Friday afternoons & Saturdays\n\n"
        f"I'll ping you when matching slots appear. Cycle searches keep running too."
    )


# ---- Scraping (runs periodically for each active watch) ----

def do_scrape(state: dict) -> None:
    now = datetime.now(timezone.utc)
    ttl = timedelta(hours=SEEN_SLOT_TTL_HOURS)
    notified_this_run = 0

    for watch in state["watches"]:
        if notified_this_run >= MAX_NOTIFICATIONS_PER_SCRAPE:
            break

        earliest = date.fromisoformat(watch["earliest_date"])
        try:
            all_slots = get_available_slots(watch["category"], earliest)
        except Exception as e:
            print(f"Scrape failed for watch {watch['id']}: {e}")
            continue

        # Apply scope (preferred tiers, or all tiers if broadened), sorted
        # most-preferred first.
        broadened = watch.get("broadened", False)
        relevant = [
            s for s in filter_by_scope(all_slots, broadened)
            if s.start.date() >= earliest
        ]

        # --- Empty-preferred nudge ---
        # If we're in preferred-only mode and there are NO preferred slots in the
        # whole calendar, surface the closest non-preferred alternatives once.
        if not broadened:
            preferred_exists = len(relevant) > 0
            if preferred_exists:
                # Situation recovered — reset so a future emptiness re-nudges.
                watch["notified_empty_preferred"] = False
            elif not watch.get("notified_empty_preferred"):
                alternatives = [
                    s for s in filter_by_scope(all_slots, broadened=True)
                    if s.start.date() >= earliest
                ][:CLOSEST_ALTERNATIVES_COUNT]
                if alternatives:
                    lines = [
                        f"🔍 No Saturday or Friday-afternoon slots for "
                        f"<b>{category_display(watch['category'])}</b> anywhere in "
                        f"Josefine's calendar right now.\n\nClosest alternatives:"
                    ]
                    for s in alternatives:
                        warn = " ⚠️ past closing" if s.runs_past_closing else ""
                        lines.append(f"• {s.display()}{warn}")
                    tg.send_message(
                        "\n".join(lines),
                        buttons=[
                            [{"text": "🔍 Broaden to all days",
                              "callback_data": f"broaden:{watch['id']}"}],
                            [{"text": "Keep waiting for preferred",
                              "callback_data": f"keep_waiting:{watch['id']}"}],
                        ],
                    )
                    watch["notified_empty_preferred"] = True

        # Refresh "last seen" timestamp for slots still available
        current_fingerprints = {s.fingerprint for s in relevant}
        for fp in current_fingerprints:
            if fp in watch["seen_slots"]:
                watch["seen_slots"][fp] = now.isoformat()

        # Prune seen slots no longer present that exceeded TTL
        to_drop = []
        for fp, last_seen_iso in watch["seen_slots"].items():
            if fp in current_fingerprints:
                continue
            try:
                last_seen = datetime.fromisoformat(last_seen_iso)
            except ValueError:
                to_drop.append(fp)
                continue
            if now - last_seen > ttl:
                to_drop.append(fp)
        for fp in to_drop:
            del watch["seen_slots"][fp]

        # Notify about slots we haven't seen before (already sorted best-first)
        backup = watch.get("backup")  # {"date": "...", "time": "..."} or None
        for slot in relevant:
            if notified_this_run >= MAX_NOTIFICATIONS_PER_SCRAPE:
                break
            if slot.fingerprint in watch["seen_slots"]:
                continue

            source_tag = "🔁" if watch["source"] == "cycle" else "⚡"
            star = "⭐ " if slot.tier in (1, 2) else ""
            warn = "\n⚠️ may run past closing" if slot.runs_past_closing else ""
            backup_note = ""
            if backup:
                backup_note = (
                    f"\n(You have a backup booked for {backup['date']} {backup['time']})"
                )

            tg.send_message(
                f"🎯 <b>New slot</b> ({source_tag} {category_display(watch['category'])})\n"
                f"{star}{slot.display()}{warn}{backup_note}",
                buttons=[[{"text": "Open in Cliento", "url": tg.cliento_link()}]],
            )
            watch["seen_slots"][slot.fingerprint] = now.isoformat()
            notified_this_run += 1


# ---- Heartbeat: weekly "still watching" status per active watch ----

def do_heartbeat(state: dict) -> None:
    now = datetime.now(timezone.utc)
    interval = timedelta(days=HEARTBEAT_DAYS)

    for watch in state["watches"]:
        last_hb = watch.get("last_heartbeat")
        if last_hb:
            try:
                if now - datetime.fromisoformat(last_hb) < interval:
                    continue
            except ValueError:
                pass
        else:
            # First heartbeat only after one full interval since creation, so we
            # don't ping right after a search starts.
            try:
                if now - datetime.fromisoformat(watch["created_at"]) < interval:
                    continue
            except (KeyError, ValueError):
                continue

        earliest = date.fromisoformat(watch["earliest_date"])
        try:
            all_slots = get_available_slots(watch["category"], earliest)
        except Exception as e:
            print(f"Heartbeat scrape failed for watch {watch['id']}: {e}")
            continue

        preferred = [s for s in filter_by_scope(all_slots, False) if s.start.date() >= earliest]
        scope = "all days" if watch.get("broadened") else "Sat + Fri afternoon"
        tag = "🔁" if watch["source"] == "cycle" else "⚡"

        if preferred:
            msg = (
                f"💓 Still watching {tag} <b>{category_display(watch['category'])}</b> "
                f"({scope}).\n{len(preferred)} preferred slot(s) currently open — "
                f"soonest {preferred[0].display()}."
            )
            tg.send_message(msg, buttons=[[{"text": "Open in Cliento", "url": tg.cliento_link()}]])
        else:
            alternatives = [
                s for s in filter_by_scope(all_slots, broadened=True)
                if s.start.date() >= earliest
            ][:CLOSEST_ALTERNATIVES_COUNT]
            lines = [
                f"💓 Still watching {tag} <b>{category_display(watch['category'])}</b> "
                f"({scope}).\nNo Saturday/Friday-afternoon slots yet."
            ]
            buttons = []
            if alternatives and not watch.get("broadened"):
                lines.append("\nClosest alternatives:")
                for s in alternatives:
                    warn = " ⚠️ past closing" if s.runs_past_closing else ""
                    lines.append(f"• {s.display()}{warn}")
                buttons = [[{"text": "🔍 Broaden to all days",
                             "callback_data": f"broaden:{watch['id']}"}]]
            tg.send_message("\n".join(lines), buttons=buttons or None)

        watch["last_heartbeat"] = now.isoformat()


# ---- Kvitto auto-start: when an appointment actually happens, auto-start next cycle ----

def do_kvitto_autostart(state: dict) -> None:
    """Check for a new kvitto (completed appointment). If found and we haven't
    already acted on it, auto-start the next cycle search and notify the user."""
    since = datetime.now(timezone.utc) - timedelta(days=10)
    completed = check_for_completed_appointment(since)
    if completed is None:
        return

    appt_date, service_name = completed
    appt_iso = appt_date.isoformat()

    # Don't act on the same kvitto twice.
    if state.get("last_autostart_kvitto") == appt_iso:
        return

    # If the service couldn't be recovered from a matching booking, don't guess —
    # just record the appointment and let the user start manually.
    if service_name is None:
        state["last_appointment"] = {"date": appt_iso, "service": None, "category": None}
        state["last_autostart_kvitto"] = appt_iso
        tg.send_message(
            f"📌 I see you had an appointment on {appt_date.strftime('%b %d')} "
            f"(receipt found), but couldn't tell which service.\n"
            f"Tap <b>Just had an appointment</b> to set up your next search."
        )
        return

    category = infer_category(service_name)
    state["last_appointment"] = {"date": appt_iso, "service": service_name, "category": category}
    state["last_autostart_kvitto"] = appt_iso

    # Update the roots/highlights streak from this completed appointment.
    update_roots_streak(state, category)

    # Only auto-start for cycle services (highlights/roots). For anything else,
    # don't presume a cycle — just acknowledge.
    if category not in CYCLE_SERVICES:
        tg.send_message(
            f"📌 Saw your receipt: <b>{service_name}</b> on {appt_date.strftime('%b %d')}.\n"
            f"That's not part of your usual cycle, so I won't auto-start a search. "
            f"Tap <b>Look for a slot</b> if you want to search for something."
        )
        return

    suggested = suggest_next_cycle_category(category, state.get("roots_since_highlights", 0))
    weeks = CYCLE_COOLDOWNS_WEEKS[suggested]
    earliest = appt_date + timedelta(weeks=weeks)
    if earliest < date.today():
        earliest = date.today()

    watch = new_watch(source="cycle", category=suggested, earliest_date=earliest.isoformat())
    add_or_replace_cycle_watch(state, watch)

    streak_note = ""
    if suggested == HIGHLIGHTS_DEFAULT_CATEGORY:
        streak_note = (
            f"\n(That's {state.get('roots_since_highlights', 0)} roots since your "
            "last highlights, so I'm switching to highlights.)"
        )

    tg.send_message(
        f"✨ Saw your receipt: <b>{service_name}</b> on {appt_date.strftime('%b %d')}.{streak_note}\n\n"
        f"I've started watching for your next <b>{category_display(suggested)}</b> "
        f"from {earliest.strftime('%b %d')} ({weeks} weeks out), Saturday + Friday afternoon.\n\n"
        f"Want something different?",
        buttons=[
            [{"text": "Change service", "callback_data": "change_cycle_service:_"}],
            [{"text": "Broaden to all days", "callback_data": f"broaden:{watch['id']}"}],
            [{"text": "Stop this search", "callback_data": f"stop_watch:{watch['id']}"}],
        ],
    )


# ---- Booking detection: backup vs final (when watches active) ----

def do_email_check(state: dict) -> None:
    if not state["watches"]:
        return

    # Look back to the oldest watch's creation time
    oldest = min(
        datetime.fromisoformat(w["created_at"]) for w in state["watches"]
    )

    booking = check_for_new_booking(oldest)
    if booking is None:
        return

    appointment_dt, service_name = booking
    category = infer_category(service_name)
    booked_date = appointment_dt.date()

    # Find all watches matching this category
    candidates = [w for w in state["watches"] if w["category"] == category]
    if not candidates:
        return

    # If multiple watches share the category, pick the one whose earliest_date
    # best fits the booked appointment. A booking should belong to the watch
    # that was actually looking for that date — i.e. the watch with the latest
    # earliest_date that's still on or before the booked date. (An ad-hoc search
    # starting today and a cycle search starting in 8 weeks: a booking for next
    # week belongs to the ad-hoc one.)
    def fit_score(w):
        try:
            ed = date.fromisoformat(w["earliest_date"])
        except (KeyError, ValueError):
            return (0, date.min)
        # Prefer watches whose window includes the booked date (earliest_date <= booked)
        includes = 1 if ed <= booked_date else 0
        return (includes, ed)

    best = max(candidates, key=fit_score)

    # Don't auto-stop. Ask whether this is the final booking (stop) or just a
    # backup placeholder (keep searching for something better).
    # Record the detected booking on the watch so we don't re-prompt for it.
    already = best.get("last_detected_booking")
    this_booking = f"{booked_date.isoformat()} {appointment_dt.strftime('%H:%M')}"
    if already == this_booking:
        return  # already asked about this exact booking
    best["last_detected_booking"] = this_booking

    tail = ""
    if best["source"] == "cycle":
        tail = " (after your visit, tap “Just had an appointment” to start the next cycle)"

    tg.send_message(
        f"📅 I see you booked <b>{service_name}</b> on "
        f"{appointment_dt.strftime('%b %d at %H:%M')}.\n\n"
        f"Is this your final booking, or a backup while you wait for something better?",
        buttons=[
            [{"text": "✅ Final — stop searching",
              "callback_data": f"booking_final:{best['id']}"}],
            [{"text": "🔖 Backup — keep looking",
              "callback_data": f"booking_backup:{best['id']}|{this_booking}"}],
        ],
    )


# ---- Main ----

def main() -> int:
    state = load_state()
    state["run_counter"] = state.get("run_counter", 0) + 1

    # 1. Always handle Telegram updates
    try:
        updates = tg.get_updates(offset=state.get("pending_telegram_offset", 0))
        for update in updates:
            state["pending_telegram_offset"] = update["update_id"] + 1
            if "message" in update:
                text = update["message"].get("text", "")
                if text:
                    handle_text_message(text, state)
            elif "callback_query" in update:
                cb = update["callback_query"]
                tg.answer_callback(cb["id"])
                if cb.get("data"):
                    handle_callback(cb["data"], state)
            save_state(state)
    except Exception as e:
        print(f"Telegram update handling failed: {e}")

    # 2. Email checks (on the email cadence)
    if state["run_counter"] % EMAIL_CHECK_EVERY_N_RUNS == 0:
        # 2a. Kvitto auto-start — runs even with no active watches, so a completed
        # appointment automatically kicks off the next cycle search.
        try:
            do_kvitto_autostart(state)
        except Exception as e:
            print(f"Kvitto auto-start failed: {e}")
        # 2b. Booking detection (backup vs final) — only when watches are active.
        if state["watches"]:
            try:
                do_email_check(state)
            except Exception as e:
                print(f"Email check failed: {e}")

    # 3. Scrape Cliento (if any watches active)
    if state["watches"] and state["run_counter"] % SCRAPE_EVERY_N_RUNS == 0:
        try:
            do_scrape(state)
        except Exception as e:
            print(f"Scrape failed: {e}")

    # 4. Weekly heartbeat (if any watches active) — runs on the scrape cadence;
    # the function itself enforces the once-per-week timing per watch.
    if state["watches"] and state["run_counter"] % SCRAPE_EVERY_N_RUNS == 0:
        try:
            do_heartbeat(state)
        except Exception as e:
            print(f"Heartbeat failed: {e}")

    save_state(state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # Last-resort failure notification so a silent crash doesn't look like
        # "no slots available". Best-effort: if even this fails, just raise.
        import traceback
        traceback.print_exc()
        try:
            tg.send_message(
                "⚠️ The hair bot hit an error on its last run and may not be "
                f"working. Error: <code>{str(e)[:300]}</code>\n\n"
                "Check the GitHub Actions logs when you get a chance."
            )
        except Exception:
            pass
        raise
