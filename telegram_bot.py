"""Telegram bot interactions — sending messages, receiving updates, handling buttons.

Uses the Telegram Bot HTTP API directly (no library dependency) so we keep
GitHub Actions runs lean.

Required env vars:
- TELEGRAM_BOT_TOKEN: from @BotFather
- TELEGRAM_CHAT_ID: your numeric chat ID (from @userinfobot)
"""

import json
import os
from typing import Optional

import requests


TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _token() -> str:
    return os.environ["TELEGRAM_BOT_TOKEN"]


def _chat_id() -> str:
    return os.environ["TELEGRAM_CHAT_ID"]


def _call(method: str, **payload) -> dict:
    url = TELEGRAM_API.format(token=_token(), method=method)
    resp = requests.post(url, json=payload, timeout=20)
    resp.raise_for_status()
    return resp.json()


def send_message(text: str, buttons: Optional[list] = None,
                 parse_mode: str = "HTML") -> None:
    """Send a message to the user. `buttons` is a list of rows, each row is a
    list of dicts with 'text' and either 'callback_data' or 'url'."""
    payload = {
        "chat_id": _chat_id(),
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    _call("sendMessage", **payload)


def set_main_menu() -> None:
    """Set the persistent reply keyboard at the bottom of the chat."""
    payload = {
        "chat_id": _chat_id(),
        "text": "Menu updated.",
        "reply_markup": {
            "keyboard": [
                [{"text": "Just had an appointment"}],
                [{"text": "Look for a slot"}],
                [{"text": "Status"}, {"text": "Stop searching"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        },
    }
    _call("sendMessage", **payload)


def get_updates(offset: int = 0) -> list:
    """Fetch new updates (messages, button presses) since the given offset."""
    url = TELEGRAM_API.format(token=_token(), method="getUpdates")
    resp = requests.get(
        url, params={"offset": offset, "timeout": 0}, timeout=20
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("result", [])


def answer_callback(callback_id: str, text: str = "") -> None:
    """Acknowledge a button press so Telegram stops showing the loading spinner.

    This is best-effort: callback queries expire quickly (within ~minutes), and
    since this bot processes updates on a polling delay, the callback is often
    already expired by the time we answer. A failure here is harmless (the only
    effect is the spinner not clearing immediately), so we never let it raise.
    """
    try:
        _call("answerCallbackQuery", callback_query_id=callback_id, text=text)
    except Exception as e:
        print(f"answerCallbackQuery failed (non-fatal): {e}")


def edit_message_text(chat_id, message_id, text: str) -> None:
    """Replace a message's text and remove its buttons (best-effort).
    Used to show the user what they picked after tapping an inline button."""
    try:
        _call("editMessageText", chat_id=chat_id, message_id=message_id,
              text=text, parse_mode="HTML",
              reply_markup={"inline_keyboard": []})
    except Exception as e:
        print(f"editMessageText failed (non-fatal): {e}")


def cliento_link() -> str:
    return "https://cliento.com/business/urban-hair-ab-urbanhair/"
