"""Regal Unlimited parser.

Regal reservation emails encode the movie title, theater name and
showtime inside an inline JPEG (``Content-ID`` attachment) — the HTML
body only contains boilerplate + an order table showing "Unlimited All
Access Tickets". The reliable machine-readable data comes from two
places:

1. The **subject line** — always parseable::

       Regal Tickets for <TITLE> #<BOOKING_CODE>

   Booking codes are 6-8 alphanumeric chars. We namespace them as
   ``regal:<CODE>`` for the orchestrator's ``order_number`` correlation
   contract so they can never collide with an AMC numeric order number.

2. The **inline JPEG ticket** — when the ``ocr`` extras are installed
   (``pip install 'ticketarr[ocr]'`` or the docker image, which ships
   with ``tesseract-ocr``) we run OCR on it to recover the true showtime
   + theater name. Typical OCR output looks like::

       Lynbrook 13 & RPX

       Backrooms
       Thursday, 28 May 2026 9:40 PM
       Seats: B8-10
       Auditorium: 3
       Order#: WJSG8RD

   If OCR is unavailable or doesn't yield a valid showtime, we fall back
   to ``datetime.now(UTC)`` so the orchestrator can still scrobble
   something reasonable. Yamtrack uses server time anyway, and
   Trakt / Ryot receive a UTC timestamp close to when the ticket was
   actually purchased.

Refund confirmations (``From: noreply@regaltickets.com``) DO include the
full showtime + theater + booking code in the body:

    Your order # <CODE> for <TITLE> at <THEATER> for <DAY, MMM D, YYYY H:MM AM/PM>
    has been refunded.

Everything else Regal sends (concession orders, friend requests,
milestone notifications, payment receipts, sneak-screening invites,
voucher nudges, deals, etc.) is classified as ``other`` with a
``skip_reason``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from .base import ParsedEmail
from .ocr import image_to_lines
from .util import body_text, clean, missing_fields_error

log = logging.getLogger(__name__)

_CHAIN = "regal"
_TICKET_SENDER = "tickets@regaltickets.com"
_REFUND_SENDER = "noreply@regaltickets.com"


# Subject: "Regal Tickets for <TITLE> #<CODE>"
_RESERVATION_SUBJECT = re.compile(
    r"^\s*Regal\s+Tickets\s+for\s+(?P<title>.+?)\s+#(?P<code>[A-Z0-9]{6,8})\s*$",
    flags=re.IGNORECASE,
)

# Body of a refund email — matches "Your order # <CODE> for <TITLE> at
# <THEATER> for <DAY, MONTH D, YYYY H:MM AM/PM> has been refunded."
_REFUND_BODY = re.compile(
    r"Your\s+order\s*#\s*(?P<code>[A-Z0-9]{6,8})\s+"
    r"for\s+(?P<title>.+?)\s+"
    r"at\s+(?P<theater>.+?)\s+"
    r"for\s+(?P<weekday>[A-Za-z]+),\s+"
    r"(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s+(?P<ampm>AM|PM)\s+"
    r"has\s+been\s+refunded",
    flags=re.IGNORECASE,
)

# Ticket-JPEG OCR: "Weekday, DD Month YYYY H:MM AM/PM" (day-first).
_OCR_SHOWTIME = re.compile(
    r"(?P<weekday>[A-Za-z]+),\s+(?P<day>\d{1,2})\s+"
    r"(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s+(?P<ampm>AM|PM)",
    flags=re.IGNORECASE,
)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Regal often uses the Unicode modifier letter triangular colon (U+02D0)
# in place of an ASCII ':' in subject lines (but not in the ticket image).
_MODIFIER_COLON = "\u02D0"

# Format prefixes Regal prepends to their subject titles that TMDB will
# never match: "IMAX:", "RPX:", "4DX:".
_FORMAT_PREFIX = re.compile(
    r"^(?:IMAX|RPX|4DX)\s*:\s*",
    flags=re.IGNORECASE,
)

# Trailing parenthesised qualifiers we drop before TMDB lookup:
#   "(Sub)", "(Dub)", "(4K Remaster)", "(3-23)", "(6-1)", "(04-06)"
_TRAILING_PARENS = re.compile(r"\s*\([^)]*\)\s*$")

# Trailing marketing suffixes: "- Early Access", "- Fan First Screenings",
# "- Live Q&A Event" etc. Only stripped when preceded by a hyphen with
# spaces on both sides (Regal's convention).
_TRAILING_SUFFIX = re.compile(
    r"\s+-\s+(?:Early\s+Access|Fan\s+First\s+Screenings?|Live\s+Q&A\s+Event|"
    r"Choose\s+Your\s+Fate[^$]*)$",
    flags=re.IGNORECASE,
)

# Cataloguing convention: "Drama, The" -> "The Drama". Applied last.
_TRAILING_ARTICLE = re.compile(r",\s+(The|A|An)\s*$", flags=re.IGNORECASE)


def _normalize_title(raw: str) -> str:
    """Best-effort cleanup so the raw title has a fighting chance against
    TMDB's search."""
    t = clean(raw).replace(_MODIFIER_COLON, ":")
    t = _FORMAT_PREFIX.sub("", t)
    while True:
        new = _TRAILING_PARENS.sub("", t)
        new = _TRAILING_SUFFIX.sub("", new)
        new = clean(new)
        if new == t:
            break
        t = new
    if m := _TRAILING_ARTICLE.search(t):
        article = m.group(1)
        t = clean(f"{article} {t[: m.start()]}")
    return t


class RegalParser:
    """Parser for Regal Unlimited reservation / refund emails."""

    chain = _CHAIN
    sender_filters = [_TICKET_SENDER, _REFUND_SENDER]

    def can_parse(
        self,
        *,
        subject: str,
        from_addr: str,
        html: Optional[str],
        text: Optional[str],
    ) -> bool:
        addr = (from_addr or "").lower()
        if "regaltickets.com" in addr:
            return True
        subj = (subject or "").lower()
        return subj.startswith("regal tickets") or "regal tickets refund" in subj

    def parse(
        self,
        *,
        subject: str,
        html: Optional[str],
        text: Optional[str],
        images: Optional[list[bytes]] = None,
    ) -> ParsedEmail:
        subject = subject or ""
        body = body_text(html, text)
        kind = _classify(subject, body)

        if kind == "reservation":
            return _parse_reservation(subject, images or [])
        if kind == "cancellation":
            return _parse_cancellation(body)
        return ParsedEmail(
            kind="other",
            ok=True,
            skip_reason=_skip_reason(subject),
            source=_CHAIN,
        )


def _classify(subject: str, body: str) -> str:
    subj = subject.lower()
    if "refund" in subj:
        return "cancellation"
    if _RESERVATION_SUBJECT.match(subject):
        return "reservation"
    return "other"


def _skip_reason(subject: str) -> str:
    subj = subject.lower()
    if "concession" in subj or "food & drink" in subj or "food and drink" in subj:
        return "concession order"
    if "friend request" in subj:
        return "friend request notification"
    if "regal unlimited pass" in subj and "seen" in subj:
        return "milestone notification"
    if "received your payment" in subj:
        return "membership payment receipt"
    if "preparing your order" in subj:
        return "concession pickup notification"
    if "sneak screening" in subj:
        return "sneak-screening invite"
    if "voucher" in subj:
        return "voucher reminder"
    if "your deal" in subj or "deal is waiting" in subj:
        return "marketing / deal"
    if "journey starts before" in subj:
        return "marketing / upsell"
    return "unrecognized Regal email"


def _parse_reservation(subject: str, images: list[bytes]) -> ParsedEmail:
    m = _RESERVATION_SUBJECT.match(subject)
    if not m:
        return ParsedEmail(
            kind="reservation",
            ok=False,
            error="could not extract title/booking code from subject",
            source=_CHAIN,
        )
    raw_title = m.group("title")
    code = m.group("code").upper()
    title = _normalize_title(raw_title)

    # Attempt OCR on each attached image; take the first that yields a
    # showtime we can parse. On failure, fall back to "now" so we still
    # scrobble.
    showtime: Optional[datetime] = None
    theater: Optional[str] = None
    for img_bytes in images:
        lines = image_to_lines(img_bytes)
        if not lines:
            continue
        ocr_theater, ocr_showtime = _read_ticket_ocr(lines, expected_code=code)
        if ocr_showtime is not None:
            showtime = ocr_showtime
            theater = ocr_theater
            break

    if showtime is None:
        showtime = datetime.now(timezone.utc)
        log.debug(
            "Regal: OCR did not yield a showtime for %s; using now() (%s)",
            code, showtime.isoformat(),
        )

    order = f"regal:{code}"

    ok = bool(title)
    return ParsedEmail(
        kind="reservation",
        ok=ok,
        error=None if ok else missing_fields_error(title=title),
        title=title,
        theater_name=theater,
        showtime=showtime,
        order_number=order,
        ticket_confirmation=code,
        source=_CHAIN,
    )


def _read_ticket_ocr(
    lines: list[str],
    *,
    expected_code: str,
) -> tuple[Optional[str], Optional[datetime]]:
    """Extract (theater, showtime) from a Regal ticket JPEG's OCR output.

    The image consistently renders as::

        <Theater name>
        <Title (possibly line-wrapped)>
        <Weekday>, <DD> <Month> <YYYY> <H:MM AM/PM>
        Seats: <...>
        Auditorium: <n>
        Order#: <CODE>

    We locate the showtime line by regex and take the very first non-empty
    line as the theater. Both are best-effort; a missing showtime means
    the caller falls back to ``now()``.
    """
    theater: Optional[str] = None
    showtime: Optional[datetime] = None

    # Theater is always the very first substantive line.
    if lines:
        theater = lines[0].strip()

    for line in lines:
        m = _OCR_SHOWTIME.search(line)
        if not m:
            continue
        month = _MONTHS.get(m.group("month").lower())
        if month is None:
            continue
        day = int(m.group("day"))
        year = int(m.group("year"))
        hour = int(m.group("hour"))
        minute = int(m.group("minute"))
        if m.group("ampm").upper() == "PM" and hour < 12:
            hour += 12
        if m.group("ampm").upper() == "AM" and hour == 12:
            hour = 0
        try:
            showtime = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        except ValueError:
            showtime = None
        break

    # If Order# appears in the OCR and doesn't roughly match the subject
    # code, don't trust the OCR result. OCR frequently mis-reads (Q<->O)
    # and inserts stray characters, so compare via longest-common-
    # subsequence rather than by position — require at least 5 shared
    # chars of the 6-8 char booking code.
    for line in lines:
        om = re.search(r"Order#?:\s*([A-Z0-9]+)", line, flags=re.IGNORECASE)
        if not om:
            continue
        ocr_code = om.group(1).upper()
        if _lcs_length(ocr_code, expected_code) < 5:
            log.debug(
                "Regal OCR: order code mismatch (image=%s subject=%s), ignoring OCR",
                ocr_code, expected_code,
            )
            return None, None
        break

    return theater, showtime


def _lcs_length(a: str, b: str) -> int:
    """Length of the longest common subsequence of two short strings.

    Used to fuzzy-match an OCR-derived booking code against the subject
    code — OCR frequently drops or inserts characters (WPSTQSG ->
    WPSTOQSG), so a positional comparison isn't tolerant enough.
    """
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for i, ca in enumerate(a, 1):
        curr = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev = curr
    return prev[-1]


def _parse_cancellation(body: str) -> ParsedEmail:
    m = _REFUND_BODY.search(body)
    if not m:
        return ParsedEmail(
            kind="cancellation",
            ok=False,
            error="could not locate refund block in Regal refund email",
            source=_CHAIN,
        )
    code = m.group("code").upper()
    title = _normalize_title(m.group("title"))
    theater = clean(m.group("theater"))

    month = _MONTHS.get(m.group("month").lower())
    day = int(m.group("day"))
    year = int(m.group("year"))
    hour = int(m.group("hour"))
    minute = int(m.group("minute"))
    if m.group("ampm").upper() == "PM" and hour < 12:
        hour += 12
    if m.group("ampm").upper() == "AM" and hour == 12:
        hour = 0

    showtime: Optional[datetime] = None
    if month is not None:
        try:
            showtime = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        except ValueError:
            showtime = None

    order = f"regal:{code}"
    return ParsedEmail(
        kind="cancellation",
        ok=True,
        title=title or None,
        theater_name=theater or None,
        showtime=showtime,
        order_number=order,
        ticket_confirmation=code,
        source=_CHAIN,
    )
