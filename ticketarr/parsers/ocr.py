"""OCR helper for parsers that need to read text out of inline images
(e.g. Regal Unlimited reservation tickets, whose title/theater/showtime are
rendered as a JPEG rather than in the HTML body).

Dependencies (``pytesseract``, ``Pillow``, and the ``tesseract-ocr`` binary)
are soft — this module gracefully returns ``None`` / empty output when they
aren't available, so parsers can fall back to whatever partial data they
already have. The container image ships with ``tesseract-ocr`` installed;
local development installs can add ``pip install 'ticketarr[ocr]'``.
"""

from __future__ import annotations

import io
import logging
import os
import re
from typing import Optional

log = logging.getLogger(__name__)

_TESSERACT_CHECKED = False
_TESSERACT_AVAILABLE = False


def _tesseract_available() -> bool:
    """Verify the tesseract binary + Python bindings are importable.

    Cached after the first call so we don't spam the log on every email.
    """
    global _TESSERACT_CHECKED, _TESSERACT_AVAILABLE
    if _TESSERACT_CHECKED:
        return _TESSERACT_AVAILABLE
    _TESSERACT_CHECKED = True
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        log.info("OCR disabled: %s (install 'ticketarr[ocr]' to enable)", exc)
        return False
    try:
        import pytesseract

        version = pytesseract.get_tesseract_version()
        log.info("OCR enabled: tesseract %s", version)
    except Exception as exc:
        log.info("OCR disabled: tesseract binary not runnable (%s)", exc)
        return False
    _TESSERACT_AVAILABLE = True
    return True


# Overlay watermark tokens Tesseract picks up from Regal's tickets that
# aren't part of the useful ticket metadata. Compared as substrings on
# each stripped line (case-insensitive).
_OVERLAY_TOKENS = (
    "receipt only",
    "not valid",
    "rot valid",  # common misread of "NOT VALID"
    "admittance",
)


def image_to_lines(image_bytes: bytes) -> list[str]:
    """OCR a single image and return non-empty, watermark-free lines.

    Returns an empty list on any error (missing deps, decode failure,
    tesseract exception, etc.) so callers can decide what to do.
    """
    if not _tesseract_available():
        return []
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return []

    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")
    except Exception as exc:
        log.debug("OCR: could not decode image: %s", exc)
        return []

    try:
        raw = pytesseract.image_to_string(img, lang="eng")
    except Exception as exc:
        log.debug("OCR: tesseract failed: %s", exc)
        return []

    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        # Drop watermark overlay tokens (they sometimes render as their own
        # line, sometimes tacked on to the end of the title line).
        for token in _OVERLAY_TOKENS:
            stripped = re.sub(rf"\s*{re.escape(token)}\.?", "", stripped, flags=re.IGNORECASE)
        stripped = stripped.strip(" .")
        # If the whole line was watermark garbage, skip it.
        if not stripped:
            continue
        # Skip "FOR" that survives after "NOT VALID FOR ADMITTANCE" trimming.
        if stripped.upper() == "FOR":
            continue
        lines.append(stripped)
        _ = low  # keep for future heuristics without triggering lint
    return lines


def choose_tesseract_binary() -> None:
    """Honor a ``TESSERACT_CMD`` env var so operators can point at a
    non-standard binary path (e.g. a bundled tesseract) without shelling
    out. No-op if the var is unset or pytesseract isn't installed."""
    cmd = os.environ.get("TESSERACT_CMD")
    if not cmd:
        return
    try:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = cmd
    except ImportError:
        pass


# Honor operator overrides at import time.
choose_tesseract_binary()


__all__ = ["image_to_lines"]
