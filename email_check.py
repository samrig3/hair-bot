"""Checks Yahoo inbox via IMAP for Cliento emails.

Two distinct email types, both from no-reply@cliento.com:

1. BOOKING CONFIRMATION ("Din bokning hos Urban Hair")
   Sent when you BOOK. Refers to a FUTURE appointment.
   Contains: future date+time, service name (Tjänst:), stylist (Person:).
   Used to detect that you've booked something (backup/final flow).

2. KVITTO / RECEIPT ("Kvitto från Urban Hair")
   Sent AFTER an appointment actually happened (you paid).
   Contains: receipt number, DATE ONLY (no time, no service), amount.
   Used to detect that an appointment actually occurred ("Just had an appointment").
   Service is recovered by matching the kvitto date back to a booking confirmation.

Requires env vars (GitHub Actions secrets):
- YAHOO_EMAIL
- YAHOO_APP_PASSWORD
"""

import os
import re
import email
import imaplib
from datetime import datetime, date
from email.header import decode_header
from typing import Optional, Tuple, List

IMAP_HOST = "imap.mail.yahoo.com"
IMAP_PORT = 993
CLIENTO_SENDER = "cliento.com"

SWEDISH_MONTHS = {
    "januari": 1, "februari": 2, "mars": 3, "april": 4,
    "maj": 5, "juni": 6, "juli": 7, "augusti": 8,
    "september": 9, "oktober": 10, "november": 11, "december": 12,
}


# ---- low-level helpers ----

def _decode(value) -> str:
    if value is None:
        return ""
    out = []
    for content, charset in decode_header(value):
        if isinstance(content, bytes):
            out.append(content.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(content)
    return "".join(out)


def _get_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    return re.sub(r"<[^>]+>", " ", html)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def _parse_swedish_datetime(text: str) -> Optional[datetime]:
    """Parse 'fredag 12 juni 2026 09:30' -> datetime (weekday name ignored)."""
    m = re.search(r"(\d{1,2})\s+([a-zåäö]+)\s+(\d{4})\s+(\d{1,2}):(\d{2})", text, re.IGNORECASE)
    if not m:
        return None
    month = SWEDISH_MONTHS.get(m.group(2).lower())
    if not month:
        return None
    try:
        return datetime(int(m.group(3)), month, int(m.group(1)), int(m.group(4)), int(m.group(5)))
    except ValueError:
        return None


def _parse_swedish_date(text: str) -> Optional[date]:
    """Parse 'lördag 28 februari 2026' -> date (no time component)."""
    m = re.search(r"(\d{1,2})\s+([a-zåäö]+)\s+(\d{4})", text, re.IGNORECASE)
    if not m:
        return None
    month = SWEDISH_MONTHS.get(m.group(2).lower())
    if not month:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None


# ---- email classification & parsing ----

def _is_kvitto(subject: str, body: str) -> bool:
    blob = (subject + " " + body).lower()
    return "kvitto" in blob


def _is_booking(subject: str, body: str) -> bool:
    blob = (subject + " " + body).lower()
    return "din bokning" in blob or "bokningsreferens" in blob


def _parse_booking(body: str) -> Optional[Tuple[datetime, str]]:
    """Return (appointment_datetime, service_name) from a booking confirmation."""
    service = None
    svc = re.search(r"Tjänst:\s*(.+?)(?:\s*Person:|\s*\n|$)", body, re.IGNORECASE)
    if svc:
        service = svc.group(1).strip()

    appt = None
    tid = re.search(r"Tidpunkt:\s*(.+?)(?:\n|Adress:|$)", body, re.IGNORECASE)
    if tid:
        appt = _parse_swedish_datetime(tid.group(1))
    if appt is None:
        appt = _parse_swedish_datetime(body)

    if appt is None or service is None:
        return None
    return appt, service


def _parse_kvitto(body: str) -> Optional[date]:
    """Return the appointment date from a kvitto (date only)."""
    d = None
    dm = re.search(r"Datum\s*(.+?)(?:\n|Betalt|$)", body, re.IGNORECASE)
    if dm:
        d = _parse_swedish_date(dm.group(1))
    if d is None:
        d = _parse_swedish_date(body)
    return d


# ---- IMAP fetch ----

def _fetch_cliento_emails(since: datetime):
    """Yield (msg, subject, body) for Cliento emails since `since`. Newest first."""
    email_addr = os.environ.get("YAHOO_EMAIL")
    password = os.environ.get("YAHOO_APP_PASSWORD")
    if not email_addr or not password:
        return

    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as imap:
        imap.login(email_addr, password)
        imap.select("INBOX")
        since_str = since.strftime("%d-%b-%Y")
        status, data = imap.search(None, f'(SINCE {since_str} FROM "{CLIENTO_SENDER}")')
        if status != "OK":
            return
        for msg_id in reversed(data[0].split()):
            status, msg_data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            if "cliento.com" not in _decode(msg.get("From", "")).lower():
                continue
            body = _get_body(msg)
            subject = _decode(msg.get("Subject", ""))
            yield msg, subject, body


# ---- public API ----

def check_for_new_booking(since: datetime) -> Optional[Tuple[datetime, str]]:
    """Find the most recent BOOKING CONFIRMATION since `since`.

    Returns (appointment_datetime, service_name) or None.
    Used for backup/final detection (you booked something).
    """
    try:
        for _msg, subject, body in _fetch_cliento_emails(since):
            if _is_kvitto(subject, body):
                continue
            if not _is_booking(subject, body):
                continue
            parsed = _parse_booking(body)
            if parsed:
                return parsed
    except Exception as e:
        print(f"Booking check failed: {e}")
    return None


def check_for_completed_appointment(since: datetime) -> Optional[Tuple[date, Optional[str]]]:
    """Find the most recent KVITTO since `since` (an appointment that happened).

    Returns (appointment_date, service_name_or_None). The service is recovered by
    matching the kvitto's date to a booking confirmation's appointment date; if no
    matching booking is found, service is None (caller should ask the user).
    """
    try:
        emails = list(_fetch_cliento_emails(since))
    except Exception as e:
        print(f"Kvitto check failed: {e}")
        return None

    # Find most recent kvitto
    kvitto_date = None
    for _msg, subject, body in emails:
        if _is_kvitto(subject, body):
            kvitto_date = _parse_kvitto(body)
            if kvitto_date:
                break
    if kvitto_date is None:
        return None

    # Try to recover the service by matching a booking confirmation on the same date
    service = None
    for _msg, subject, body in emails:
        if _is_kvitto(subject, body):
            continue
        if not _is_booking(subject, body):
            continue
        parsed = _parse_booking(body)
        if parsed and parsed[0].date() == kvitto_date:
            service = parsed[1]
            break

    return kvitto_date, service
