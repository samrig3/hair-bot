# Hair Appointment Bot

Personal Telegram bot that watches Cliento for available appointment slots with Josefine at Urban Hair (Stockholm).

## How it works

Runs every 10 minutes via GitHub Actions. Two kinds of watches can run in parallel:

- **🔁 Cycle search** — your highlights → roots → roots rotation. Started by tapping "Just had an appointment." Reads your Yahoo inbox to find your last Cliento confirmation, suggests the next cycle service, and watches for slots starting after the cooldown.
- **⚡ Ad-hoc search** — any other service you want now. Started by tapping "Look for a slot." No cooldown, searches from today.

Multiple ad-hoc searches can run alongside the cycle search. When you book something, the bot detects it from the Cliento confirmation email and stops the matching search automatically.

## Time windows

Only notifies about Friday 14:00–18:00 and Saturday 10:30–15:00 slots, with Josefine, for the service you're watching.

No upper date bound — if Cliento publishes a Saturday slot 4 months out, you'll know.

## Files

- `bot.py` — Main orchestrator. Runs each GitHub Actions trigger.
- `cliento.py` — Cliento scraper (currently a placeholder — needs the real API endpoint).
- `email_check.py` — Yahoo IMAP check for booking confirmations.
- `telegram_bot.py` — Telegram API helpers.
- `state.py` — Persistent state (multi-watch model).
- `config.py` — Cooldowns, preferred times, run cadence.
- `state.json` — Live state, committed back by Actions on each run.

## Required GitHub Actions secrets

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `YAHOO_EMAIL`
- `YAHOO_APP_PASSWORD`
