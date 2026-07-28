"""Tests for the AMC parser and the top-level parse_email dispatcher.

These are parametrized over every ``.eml`` file dropped into
``tests/fixtures/amc/``. Parameters are generated at *collection* time by
``pytest_generate_tests`` in ``conftest.py`` — so with zero fixtures the
tests simply aren't collected (no empty-parameter-set SKIP noise). Once you
add real emails, every matching file becomes its own test case, named after
the filename in pytest output.

Filename convention (see ``tests/fixtures/amc/README.md``):

- ``reservation_*.eml``  — AMC reservation / order confirmation
- ``cancellation_*.eml`` — AMC A-List reservation cancellation / refund
- ``thankyou_*.eml``     — AMC "Thank You for Visiting" post-visit email
- ``skip_*.eml``         — non-ticket order (concessions, membership, Popcorn
                           Pass, etc.) that the parser must classify as
                           ``other`` with ``ok=True`` and a ``skip_reason``.

Optional companion JSON metadata for finer assertions:
For ``<name>.eml`` you may add ``<name>.expected.json`` with any subset of:

    {
      "order_number": "123456789",
      "title": "Dune: Part Two",
      "theater_name": "AMC Foo 12",
      "showtime": "2024-03-14T22:30:00+00:00",
      "skip_reason": "concession order"
    }
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from ticketarr.parsers import parse_email
from ticketarr.parsers.amc import AMCParser
from tests.conftest import LoadedEmail, load_eml


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _expected(path: Path) -> dict[str, Any]:
    """Return the sidecar ``<name>.expected.json`` for a fixture, or ``{}``."""
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


@pytest.mark.amc_reservations
def test_amc_reservation(path: Path) -> None:
    loaded = load_eml(path)
    parsed = AMCParser().parse(subject=loaded.subject, html=loaded.html, text=loaded.text)

    assert parsed.source == "amc"
    assert parsed.kind == "reservation", f"expected reservation, got {parsed.kind}"
    assert parsed.ok, f"parser failed: {parsed.error}"
    assert parsed.title, "title should be populated"
    assert parsed.theater_name, "theater_name should be populated"
    assert parsed.order_number, "order_number should be populated"
    assert isinstance(parsed.showtime, datetime), "showtime should be a datetime"

    expected = _expected(path)
    for key in ("order_number", "title", "theater_name", "showtime"):
        _assert_matches(getattr(parsed, key), expected, key)


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #


@pytest.mark.amc_cancellations
def test_amc_cancellation(path: Path) -> None:
    loaded = load_eml(path)
    parsed = AMCParser().parse(subject=loaded.subject, html=loaded.html, text=loaded.text)

    assert parsed.source == "amc"
    assert parsed.kind == "cancellation", f"expected cancellation, got {parsed.kind}"
    assert parsed.ok, f"parser failed: {parsed.error}"
    assert parsed.order_number, "order_number is required for cancellations"

    expected = _expected(path)
    _assert_matches(parsed.order_number, expected, "order_number")
    _assert_matches(parsed.title, expected, "title")
    _assert_matches(parsed.theater_name, expected, "theater_name")


# --------------------------------------------------------------------------- #
# Thank-you
# --------------------------------------------------------------------------- #


@pytest.mark.amc_thankyous
def test_amc_thank_you(path: Path) -> None:
    loaded = load_eml(path)
    parsed = AMCParser().parse(subject=loaded.subject, html=loaded.html, text=loaded.text)

    assert parsed.source == "amc"
    assert parsed.kind == "thank_you", f"expected thank_you, got {parsed.kind}"
    assert parsed.ok, f"parser failed: {parsed.error}"
    assert parsed.title and parsed.theater_name


# --------------------------------------------------------------------------- #
# Non-ticket orders (must classify as "other" with a skip reason, not error)
# --------------------------------------------------------------------------- #


@pytest.mark.amc_skips
def test_amc_skips_non_ticket_orders(path: Path) -> None:
    loaded = load_eml(path)
    parsed = AMCParser().parse(subject=loaded.subject, html=loaded.html, text=loaded.text)

    assert parsed.kind == "other"
    assert parsed.ok, "skip results should be ok=True (informational, not an error)"
    assert parsed.skip_reason, "expected a skip_reason for a non-ticket AMC email"

    expected = _expected(path)
    _assert_matches(parsed.skip_reason, expected, "skip_reason")


# --------------------------------------------------------------------------- #
# Registry-level dispatch: parse_email() should route every AMC fixture into
# the AMC parser purely via can_parse() (subject / from_addr / body sniffing).
# --------------------------------------------------------------------------- #


@pytest.mark.amc_all
def test_registry_routes_to_amc(path: Path) -> None:
    loaded: LoadedEmail = load_eml(path)
    parsed = parse_email(
        subject=loaded.subject,
        html=loaded.html,
        text=loaded.text,
        from_addr=loaded.from_addr,
    )
    assert parsed.source == "amc", (
        f"{loaded.name}: parse_email did not route to AMC (source={parsed.source})"
    )
