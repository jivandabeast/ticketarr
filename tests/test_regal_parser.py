"""Tests for the Regal parser and the top-level parse_email dispatcher.

Parametrized over every ``.eml`` file dropped into ``tests/fixtures/regal/``.
See ``tests/conftest.py`` and ``tests/test_amc_parser.py`` for the shared
harness. Filename convention:

- ``reservation_*.eml``  — Regal Tickets confirmation ("Regal Tickets for
                           <TITLE> #<CODE>")
- ``cancellation_*.eml`` — Regal Tickets Refund Confirmation
- ``skip_*.eml``         — anything else Regal sends (concession orders,
                           friend requests, milestones, payments, sneak
                           screenings, voucher reminders, deals, etc.)

Optional companion ``<name>.expected.json`` fields (all subsets welcome):

    {
      "order_number": "regal:WJSG8RD",
      "ticket_confirmation": "WJSG8RD",
      "title": "Backrooms",
      "theater_name": "Westbury Stm 12 IMAX & RPX",
      "showtime": "2026-07-17T15:00:00+00:00",
      "skip_reason": "concession order"
    }

Note: Regal reservation emails do not carry the true showtime in a
machine-readable form (it lives inside an inline JPEG), so we don't
assert on ``showtime`` for reservations — the parser fills it with
``datetime.now(UTC)`` as a best-effort fallback.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from ticketarr.parsers import parse_email
from ticketarr.parsers.ocr import _tesseract_available
from ticketarr.parsers.regal import RegalParser
from tests.conftest import LoadedEmail, load_eml


def _expected(path: Path) -> dict[str, Any]:
    sidecar = path.parent / (path.stem + ".expected.json")
    if not sidecar.exists():
        return {}
    return json.loads(sidecar.read_text(encoding="utf-8"))


def _assert_matches(actual: Any, expected_json: dict[str, Any], key: str) -> None:
    if key not in expected_json:
        return
    expected = expected_json[key]
    if key == "showtime" and actual is not None:
        assert actual.isoformat() == expected, f"{key}: {actual.isoformat()!r} != {expected!r}"
    else:
        assert actual == expected, f"{key}: {actual!r} != {expected!r}"


# --------------------------------------------------------------------------- #
# Reservation
# --------------------------------------------------------------------------- #


@pytest.mark.regal_reservations
def test_regal_reservation(path: Path) -> None:
    loaded = load_eml(path)
    parsed = RegalParser().parse(
        subject=loaded.subject, html=loaded.html, text=loaded.text, images=loaded.images
    )

    assert parsed.source == "regal"
    assert parsed.kind == "reservation", f"expected reservation, got {parsed.kind}"
    assert parsed.ok, f"parser failed: {parsed.error}"
    assert parsed.title, "title should be populated"
    assert parsed.order_number, "order_number should be populated"
    assert parsed.order_number.startswith("regal:"), (
        f"expected namespaced order_number, got {parsed.order_number!r}"
    )
    assert parsed.ticket_confirmation, "ticket_confirmation (booking code) should be populated"
    # Showtime is either OCR-derived (when tesseract is installed) or a
    # ``datetime.now(UTC)`` fallback. Either way, it must be a datetime.
    assert isinstance(parsed.showtime, datetime)

    expected = _expected(path)
    # ``theater_name`` and ``showtime`` on reservations are OCR-derived —
    # if tesseract isn't installed the parser falls back to ``now()`` and
    # can't reproduce the sidecar values, so we skip those keys.
    keys = ["order_number", "ticket_confirmation", "title"]
    if _tesseract_available():
        keys += ["theater_name", "showtime"]
    for key in keys:
        _assert_matches(getattr(parsed, key), expected, key)


# --------------------------------------------------------------------------- #
# Cancellation (refund confirmation)
# --------------------------------------------------------------------------- #


@pytest.mark.regal_cancellations
def test_regal_cancellation(path: Path) -> None:
    loaded = load_eml(path)
    parsed = RegalParser().parse(subject=loaded.subject, html=loaded.html, text=loaded.text)

    assert parsed.source == "regal"
    assert parsed.kind == "cancellation", f"expected cancellation, got {parsed.kind}"
    assert parsed.ok, f"parser failed: {parsed.error}"
    assert parsed.order_number, "order_number is required for cancellations"
    assert parsed.order_number.startswith("regal:")

    expected = _expected(path)
    for key in ("order_number", "ticket_confirmation", "title", "theater_name", "showtime"):
        _assert_matches(getattr(parsed, key), expected, key)


# --------------------------------------------------------------------------- #
# Skips
# --------------------------------------------------------------------------- #


@pytest.mark.regal_skips
def test_regal_skips_non_ticket_orders(path: Path) -> None:
    loaded = load_eml(path)
    parsed = RegalParser().parse(subject=loaded.subject, html=loaded.html, text=loaded.text)

    assert parsed.kind == "other"
    assert parsed.ok, "skip results should be ok=True (informational, not an error)"
    assert parsed.skip_reason, "expected a skip_reason for a non-ticket Regal email"

    expected = _expected(path)
    _assert_matches(parsed.skip_reason, expected, "skip_reason")


# --------------------------------------------------------------------------- #
# Registry dispatch
# --------------------------------------------------------------------------- #


@pytest.mark.regal_all
def test_registry_routes_to_regal(path: Path) -> None:
    loaded: LoadedEmail = load_eml(path)
    parsed = parse_email(
        subject=loaded.subject,
        html=loaded.html,
        text=loaded.text,
        from_addr=loaded.from_addr,
        images=loaded.images,
    )
    assert parsed.source == "regal", (
        f"{loaded.name}: parse_email did not route to Regal (source={parsed.source})"
    )
