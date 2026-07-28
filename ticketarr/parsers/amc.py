"""AMC A-List parser.

Ported from the reference Marquee project by @ijoshi129
(https://github.com/ijoshi129/Marquee/tree/main/server/parsers). Keep this
attribution intact when refactoring — see AGENTS.md.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from .base import ParsedEmail
from .util import (
    body_text,
    clean,
    missing_fields_error,
    parse_date_then_24hr,
    parse_time_then_date,
)

_CHAIN = "amc"
_DEFAULT_SENDER = "AMCTheatres@amctheatres.com"


# Anchor on the block AMC uses inside "Ticket Purchase/Reservation Details":
#   Ticket Confirmation#: <num>
#   <Title>
#   <H:MM AM|PM> <M/D/YYYY>
#   <AMC Theater>
_RESERVATION_BLOCK = re.compile(
    r"Ticket\s+Confirmation#?:\s*(?P<ticket>\d+)\s+"
    r"(?P<title>.+?)\s+"
    r"(?P<time>\d{1,2}:\d{2}\s*[AP]M)\s+"
    r"(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<theater>AMC\s+.+?)"
    r"\s+(?:The\s+listed\s+showtime|Auditorium:|Reserved\s+Seats|Photo\s+ID|$)",
    flags=re.IGNORECASE,
)

_REFUND_BLOCK = re.compile(
    r"Confirmation\s*#:\s*(?P<ticket>\d+)\s+"
    r"Refund\s+Date:\s+\d{1,2}/\d{1,2}/\d{4}\s+"
    r"(?P<title>.+?)\s+"
    r"(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<time>\d{1,2}:\d{2})\s+"
    r"(?P<theater>AMC\s+[^.\n]+?)\s+"
    r"Refunded\s+Tickets",
    flags=re.IGNORECASE,
)

_THEATER_RE = re.compile(
    r"Thank\s*You\s+for\s+Visiting\s+(AMC\s+[^.]+?)\s+(?:We\s+hope|Don'?t\s+Miss|This\s+email|$)",
    flags=re.IGNORECASE,
)
_TITLE_RE = re.compile(
    r"We\s+hope\s+you\s+(?:enjoyed\s+seeing|liked|loved)\s+(.+?)(?:\s+Don'?t\s+Miss|\s+This\s+email|\s+Sent\s+from|$)",
    flags=re.IGNORECASE,
)
_SUBJECT_TITLE_RE = re.compile(
    r"thank\s*you\s+for\s+(?:seeing|visiting|watching)\s+(.+?)\s+at\s+amc\b",
    flags=re.IGNORECASE,
)


class AMCParser:
    """Parser for AMC A-List reservation / cancellation / thank-you emails."""

    chain = _CHAIN
    sender_filters = [_DEFAULT_SENDER]

    def can_parse(
        self,
        *,
        subject: str,
        from_addr: str,
        html: Optional[str],
        text: Optional[str],
    ) -> bool:
        haystack = f"{from_addr} {subject}".lower()
        if "amctheatres" in haystack or "amc theatres" in haystack:
            return True
        # As a last resort, sniff the body for an AMC anchor.
        body = body_text(html, text).lower()
        return "amc " in body and (
            "ticket confirmation" in body or "thank you for visiting" in body
        )

    def parse(
        self,
        *,
        subject: str,
        html: Optional[str],
        text: Optional[str],
    ) -> ParsedEmail:
        subject = subject or ""
        body = body_text(html, text)
        kind = _classify(subject, body)

        if kind == "reservation":
            return _parse_reservation(subject, html, text)
        if kind == "cancellation":
            return _parse_cancellation(subject, html, text)
        if kind == "thank_you":
            return _parse_thank_you(subject, html, text)
        return ParsedEmail(
            kind="other", ok=True, skip_reason="unrecognized AMC email", source=_CHAIN
        )


def _classify(subject: str, body: str) -> str:
    subj = subject.lower()
    b = body.lower()
    if "reservation cancelation" in subj or "reservation cancellation" in subj:
        return "cancellation"
    if "refund" in subj and "amc" in subj:
        return "cancellation"
    # "Your Refund Receipt" (no "AMC" in the subject). Fall back to the
    # body-level markers AMC always includes on a refund email.
    if "refund" in subj and "refund date" in b and "refunded tickets" in b:
        return "cancellation"
    if "ticket reservation details" in b or "ticket purchase details" in b:
        return "reservation"
    if "thank you for visiting" in b or "thank you for seeing" in subj:
        return "thank_you"
    return "other"


def _parse_reservation(subject: str, html: Optional[str], text: Optional[str]) -> ParsedEmail:
    body = body_text(html, text)

    if not re.search(r"Ticket\s+(?:Purchase|Reservation)\s+Details", body, re.IGNORECASE):
        reason = "non-ticket order"
        if re.search(r"Food\s+and\s+Drink\s+Purchase\s+Details", body, re.IGNORECASE):
            reason = "concession order"
        elif "a-list" in body.lower() and re.search(r"Membership\s+Total", body, re.IGNORECASE):
            reason = "A-List membership receipt"
        elif re.search(r"Popcorn\s+Pass", body, re.IGNORECASE):
            reason = "AMC Popcorn Pass"
        return ParsedEmail(kind="other", ok=True, skip_reason=reason, source=_CHAIN)

    title = theater = ticket = None
    showtime: Optional[datetime] = None
    block = _RESERVATION_BLOCK.search(body)
    if block:
        ticket = block.group("ticket")
        title = clean(block.group("title"))
        theater = clean(block.group("theater"))
        showtime = parse_time_then_date(f"{block.group('time')} {block.group('date')}")

    order_m = re.search(r"Order\s+Number:\s*(\d+)", body, re.IGNORECASE)
    order = order_m.group(1) if order_m else None

    ok = bool(title and theater and showtime and order)
    return ParsedEmail(
        kind="reservation",
        ok=ok,
        error=None
        if ok
        else missing_fields_error(
            title=title, theater_name=theater, showtime=showtime, order_number=order
        ),
        title=title,
        theater_name=theater,
        showtime=showtime,
        order_number=order,
        ticket_confirmation=ticket,
        source=_CHAIN,
    )


def _parse_cancellation(subject: str, html: Optional[str], text: Optional[str]) -> ParsedEmail:
    body = body_text(html, text)

    order_m = re.search(r"Order\s+Number:\s*(\d+)", body, re.IGNORECASE)
    order = order_m.group(1) if order_m else None

    title = theater = ticket = None
    showtime: Optional[datetime] = None
    block = _REFUND_BLOCK.search(body)
    if block:
        ticket = block.group("ticket")
        title = clean(block.group("title"))
        theater = clean(block.group("theater"))
        showtime = parse_date_then_24hr(f"{block.group('date')} {block.group('time')}")

    ok = bool(order)
    return ParsedEmail(
        kind="cancellation",
        ok=ok,
        error=None if ok else "missing required field: order_number",
        title=title,
        theater_name=theater,
        showtime=showtime,
        order_number=order,
        ticket_confirmation=ticket,
        source=_CHAIN,
    )


def _parse_thank_you(subject: str, html: Optional[str], text: Optional[str]) -> ParsedEmail:
    body = body_text(html, text)
    theater = None
    if m := _THEATER_RE.search(body):
        theater = clean(m.group(1))
    title = None
    if m := _TITLE_RE.search(body):
        title = clean(m.group(1))
    if (not title or title == title.upper()) and subject:
        if sm := _SUBJECT_TITLE_RE.search(subject):
            subj_title = clean(sm.group(1))
            if not title or not any(c.islower() for c in title):
                title = title if title and any(c.islower() for c in title) else subj_title

    ok = bool(title and theater)
    return ParsedEmail(
        kind="thank_you",
        ok=ok,
        error=None if ok else missing_fields_error(title=title, theater_name=theater),
        title=title,
        theater_name=theater,
        source=_CHAIN,
    )
