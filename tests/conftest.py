"""Shared pytest helpers.

The heart of the test suite is loading real ``.eml`` fixtures and running them
through the parser registry the same way ``imap_monitor`` would.

Drop sanitized real AMC emails into ``tests/fixtures/amc/`` — see the README
in that directory for filename conventions. Any ``.eml`` file present will be
picked up automatically by the parametrized tests in ``test_amc_parser.py``.
"""

from __future__ import annotations

import email
from dataclasses import dataclass
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class LoadedEmail:
    path: Path
    subject: str
    from_addr: str
    html: Optional[str]
    text: Optional[str]

    @property
    def name(self) -> str:
        return self.path.name


def _decode(raw: Optional[str]) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def _bodies(msg: EmailMessage) -> tuple[Optional[str], Optional[str]]:
    text_part: Optional[str] = None
    html_part: Optional[str] = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition") or "")
        if "attachment" in disp.lower():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except LookupError:
            decoded = payload.decode("utf-8", errors="replace")
        if ctype == "text/plain" and text_part is None:
            text_part = decoded
        elif ctype == "text/html" and html_part is None:
            html_part = decoded
    return text_part, html_part


def load_eml(path: Path) -> LoadedEmail:
    raw = path.read_bytes()
    msg: EmailMessage = email.message_from_bytes(raw, policy=policy.default)
    text, html = _bodies(msg)
    return LoadedEmail(
        path=path,
        subject=_decode(msg.get("Subject")),
        from_addr=_decode(msg.get("From")),
        html=html,
        text=text,
    )


def discover_fixtures(chain: str, kind_prefix: str) -> list[Path]:
    """Return every ``.eml`` under ``fixtures/<chain>/`` whose filename starts
    with ``kind_prefix``. Ordering is stable (sorted)."""
    directory = FIXTURES / chain
    if not directory.exists():
        return []
    return sorted(p for p in directory.glob("*.eml") if p.name.startswith(kind_prefix))


# Test functions can opt into dynamic ``.eml`` parametrization by declaring a
# ``path`` argument and one of the marker names below. Parameters are generated
# at collection time, so **zero fixtures = zero collected items** (no "empty
# parameter set" SKIP noise).
_FIXTURE_KIND_BY_MARKER = {
    "amc_reservations": ("amc", "reservation_"),
    "amc_cancellations": ("amc", "cancellation_"),
    "amc_thankyous": ("amc", "thankyou_"),
    "amc_skips": ("amc", "skip_"),
    "amc_all": ("amc", ""),  # every .eml under fixtures/amc/
}


def pytest_configure(config) -> None:  # type: ignore[no-untyped-def]
    for marker in _FIXTURE_KIND_BY_MARKER:
        config.addinivalue_line(
            "markers", f"{marker}: parametrize `path` over matching AMC .eml fixtures"
        )


def pytest_generate_tests(metafunc) -> None:  # type: ignore[no-untyped-def]
    if "path" not in metafunc.fixturenames:
        return
    for marker in metafunc.definition.iter_markers():
        spec = _FIXTURE_KIND_BY_MARKER.get(marker.name)
        if spec is None:
            continue
        chain, prefix = spec
        paths = discover_fixtures(chain, prefix)
        metafunc.parametrize("path", paths, ids=[p.name for p in paths])
        return
