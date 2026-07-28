"""Small helpers shared by all chain-specific parsers."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup

_ZERO_WIDTH = re.compile(r"[\u200B-\u200D\uFEFF]")
_NBSP = re.compile(r"\u00A0")
_WHITESPACE = re.compile(r"\s+")


def clean(text: Optional[str]) -> str:
    """Strip zero-width chars, collapse whitespace, and trim."""
    if not text:
        return ""
    text = _ZERO_WIDTH.sub("", text)
    text = _NBSP.sub(" ", text)
    text = _WHITESPACE.sub(" ", text)
    return text.strip()


def body_text(html: Optional[str], text: Optional[str]) -> str:
    """Cleaned single-line body text. Prefers HTML if provided."""
    if html:
        soup = BeautifulSoup(html, "html.parser")
        body = soup.body or soup
        return clean(body.get_text(separator=" "))
    return clean(text or "")


def parse_time_then_date(text: str) -> Optional[datetime]:
    """Parse ``H:MM AM|PM M/D/YYYY`` into a UTC datetime."""
    m = re.match(
        r"(\d{1,2}):(\d{2})\s*(AM|PM)\s+(\d{1,2})/(\d{1,2})/(\d{4})",
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    ampm = m.group(3).upper()
    if ampm == "PM" and hour < 12:
        hour += 12
    if ampm == "AM" and hour == 12:
        hour = 0
    month = int(m.group(4))
    day = int(m.group(5))
    year = int(m.group(6))
    try:
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_date_then_24hr(text: str) -> Optional[datetime]:
    """Parse ``MM/DD/YYYY HH:MM`` (24-hour) into a UTC datetime."""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})", text)
    if not m:
        return None
    month, day, year, hour, minute = (int(m.group(i)) for i in range(1, 6))
    try:
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None


def missing_fields_error(**required: object) -> str:
    missing = [k for k, v in required.items() if not v]
    return "missing required fields: " + ", ".join(missing)
