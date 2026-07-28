"""Shared parser types and the ``EmailParser`` protocol.

Every theater-chain parser must:

- Implement :class:`EmailParser`.
- Return a :class:`ParsedEmail` whose ``kind`` is one of ``EmailKind``.
- Populate an ``order_number`` (or another stable per-reservation id in the
  same field) for reservations *and* their cancellations, so the orchestrator
  can undo the matching scrobble on a cancellation.

Add the parser instance to ``ticketarr.parsers.REGISTRY``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional, Protocol

EmailKind = Literal["reservation", "cancellation", "thank_you", "other"]


@dataclass
class ParsedEmail:
    kind: EmailKind
    ok: bool
    error: Optional[str] = None
    title: Optional[str] = None
    theater_name: Optional[str] = None
    showtime: Optional[datetime] = None  # UTC
    order_number: Optional[str] = None
    ticket_confirmation: Optional[str] = None
    skip_reason: Optional[str] = None
    # Chain that produced this result (e.g. "amc", "regal"). Purely informational.
    source: Optional[str] = None


class EmailParser(Protocol):
    """A parser for one theater-chain's confirmation emails."""

    #: Short identifier for the chain, e.g. ``"amc"`` or ``"regal"``. Used in
    #: logs and in ``ParsedEmail.source``.
    chain: str

    #: Canonical From-address(es) this parser expects. The IMAP monitor uses
    #: the union of all parsers' sender_filters to build its ``FROM`` search.
    sender_filters: list[str]

    def can_parse(
        self,
        *,
        subject: str,
        from_addr: str,
        html: Optional[str],
        text: Optional[str],
    ) -> bool: ...

    def parse(
        self,
        *,
        subject: str,
        html: Optional[str],
        text: Optional[str],
    ) -> ParsedEmail: ...
