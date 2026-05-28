"""Static configuration for the hair appointment bot."""

# ---- Stylist ----
STYLIST_NAME = "JOSEFINE"
STYLIST_RESOURCE_ID = 6505   # Josefine's Cliento resource ID (resIds=6505)

# ---- Service IDs (srvIds) ----
SERVICE_IDS = {
    "roots_cut":                39416,  # ROOTS (FÄRG UTVÄXT) INKL KLIPP
    "roots":                    4778,   # ROOTS (FÄRG UTVÄXT)
    "highlights_uncolored_cut": 4781,   # SLINGOR (PÅ OFÄRGAT HÅR) INKL KLIPP
    "highlights_colored_cut":   24220,  # SLINGOR (PÅ FÄRGAT HÅR) INKL KLIPP
    "haircut":                  29608,  # HAIRCUT (KLIPPNING)
    "haircut_treatment":        29472,  # HAIRCUT + K18/EPRES
    "wash_style":               4771,   # TVÄTT OCH STYLE
}

# ---- Preference tiers ----
# Slots are ranked by tier (lower number = more preferred). The bot notifies
# about tier 1-2 by default ("preferred" scope). When a search is "broadened",
# it also notifies about tiers 3-4.
#
# Tier 1: Saturday, any time
# Tier 2: Friday afternoon (14:00-18:00)
# Tier 3: Other weekday afternoons (Mon-Thu, 12:00 onward)
# Tier 4: Anything else (early weekday mornings, etc.)
#
# weekday: 0 = Mon ... 6 = Sun
def slot_tier(weekday: int, hour: int, minute: int = 0) -> int:
    minutes = hour * 60 + minute
    if weekday == 5:                      # Saturday — any time
        return 1
    if weekday == 4 and 14 * 60 <= minutes < 18 * 60:   # Friday afternoon
        return 2
    if weekday in (0, 1, 2, 3) and minutes >= 12 * 60:  # Mon-Thu afternoon
        return 3
    return 4

# Tiers considered "preferred" (notified about even in default scope)
PREFERRED_TIERS = {1, 2}
# Tiers added when a search is broadened
BROADENED_TIERS = {1, 2, 3, 4}

# ---- Cooldowns (cycle services only), in weeks ----
CYCLE_COOLDOWNS_WEEKS = {
    "highlights_uncolored_cut": 12,
    "highlights_colored_cut":   12,
    "roots_cut":                8,
    "roots":                    8,
}
CYCLE_SERVICES = set(CYCLE_COOLDOWNS_WEEKS.keys())

# After this many roots appointments in a row, the bot suggests highlights next
# instead of another roots. Your pattern: highlights → roots → roots → highlights.
ROOTS_BEFORE_HIGHLIGHTS = 2

# Which highlights service the bot suggests when it's time (your usual one).
HIGHLIGHTS_DEFAULT_CATEGORY = "highlights_uncolored_cut"

# ---- Salon hours (for duration-fit flagging) ----
# Approximate closing time per weekday (24h hour). Used to flag slots that
# would run past closing. weekday: closing_hour
SALON_CLOSING_HOUR = {
    0: 18, 1: 18, 2: 18, 3: 18, 4: 18,   # Mon-Fri ~18:00
    5: 15,                                 # Saturday ~15:00
}

# ---- Run cadence (must align with cron in .github/workflows/bot.yml) ----
SCRAPE_EVERY_N_RUNS = 1        # ~every 2 hours at 10-min cron
EMAIL_CHECK_EVERY_N_RUNS = 144  # ~once a day

# ---- Dedup / notification tuning ----
SEEN_SLOT_TTL_HOURS = 24
MAX_NOTIFICATIONS_PER_SCRAPE = 8

# ---- Heartbeat / status ----
HEARTBEAT_DAYS = 7              # weekly "still watching" message per active watch
CLOSEST_ALTERNATIVES_COUNT = 3  # how many non-preferred slots to show when preferred is empty
