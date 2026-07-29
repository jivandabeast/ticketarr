"""Email parsers.

Each theater chain gets its own module implementing :class:`EmailParser`.
Parsers are registered in :data:`REGISTRY`; new chains only need to add a
module and one registration line — no changes to the orchestrator, IMAP
monitor, or config are required.

Currently implemented:
- :mod:`ticketarr.parsers.amc` — AMC A-List (ported from @ijoshi129/Marquee)
- :mod:`ticketarr.parsers.regal` — Regal Unlimited
"""

from __future__ import annotations

from typing import Optional

from .base import EmailKind, EmailParser, ParsedEmail
from .amc import AMCParser
from .regal import RegalParser

# Order matters only when two parsers both claim ``can_parse=True``; the first
# match wins. Prefer keeping chain-specific parsers here and let each parser's
# ``can_parse`` guard on distinctive sender / subject / body markers.
REGISTRY: list[EmailParser] = [
    AMCParser(),
    RegalParser(),
]


def parse_email(
    subject: str,
    html: Optional[str],
    text: Optional[str],
    *,
    from_addr: str = "",
    images: Optional[list[bytes]] = None,
) -> ParsedEmail:
    """Route an email to the first parser that claims it.

    Returns a ``ParsedEmail`` with ``kind='other'`` when no parser matches, so
    callers can rely on a uniform result type. Never raises.
    """
    for parser in REGISTRY:
        try:
            if parser.can_parse(subject=subject, from_addr=from_addr, html=html, text=text):
                return parser.parse(subject=subject, html=html, text=text, images=images)
        except Exception as exc:  # pragma: no cover - defensive
            return ParsedEmail(
                kind="other",
                ok=False,
                error=f"{parser.chain} parser raised: {exc}",
            )
    return ParsedEmail(kind="other", ok=True, skip_reason="no matching parser")


def default_sender_filters() -> list[str]:
    """Aggregate sender-address filters from every registered parser."""
    seen: dict[str, None] = {}
    for parser in REGISTRY:
        for addr in parser.sender_filters:
            seen.setdefault(addr, None)
    return list(seen)


__all__ = [
    "EmailKind",
    "EmailParser",
    "ParsedEmail",
    "REGISTRY",
    "parse_email",
    "default_sender_filters",
]
