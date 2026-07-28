"""IMAP monitor.

Polls (or IDLEs on) an IMAP folder for new messages from AMC and yields them
one at a time. Uses IMAPClient which supports RFC 2177 IDLE cleanly. All
blocking IMAP work runs in a background thread executor so we don't block the
asyncio loop.
"""

from __future__ import annotations

import asyncio
import email
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from typing import AsyncIterator, Optional

from imapclient import IMAPClient

from .config import IMAPConfig

log = logging.getLogger(__name__)


@dataclass
class InboundEmail:
    fingerprint: str  # stable id for dedupe (Message-ID, or uidvalidity/uid fallback)
    subject: str
    from_addr: str
    date: Optional[datetime]
    text: Optional[str]
    html: Optional[str]
    uid: int


def _decode(raw: Optional[str]) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:  # pragma: no cover - defensive
        return raw


def _extract_bodies(msg: Message) -> tuple[Optional[str], Optional[str]]:
    text_part: Optional[str] = None
    html_part: Optional[str] = None
    for part in msg.walk():
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition") or "")
        if "attachment" in disp.lower():
            continue
        if ctype == "text/plain" and text_part is None:
            text_part = _decode_payload(part)
        elif ctype == "text/html" and html_part is None:
            html_part = _decode_payload(part)
    return text_part, html_part


def _decode_payload(part: Message) -> Optional[str]:
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # pragma: no cover
        return None
    if payload is None:
        return None
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


class IMAPMonitor:
    def __init__(self, cfg: IMAPConfig, sender_filters: list[str]) -> None:
        if not sender_filters:
            raise ValueError("IMAPMonitor requires at least one sender_filter")
        self._cfg = cfg
        self._sender_filters = sender_filters
        self._stop = asyncio.Event()
        self._new_message_event = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()
        self._new_message_event.set()

    # ---- iteration --------------------------------------------------------

    async def stream(self) -> AsyncIterator[InboundEmail]:
        """Yield new AMC emails as they arrive."""
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                messages = await loop.run_in_executor(None, self._fetch_batch)
            except Exception as exc:  # pragma: no cover - runtime robustness
                log.exception("IMAP fetch failed: %s", exc)
                await self._sleep(self._cfg.poll_interval_seconds)
                continue

            for m in messages:
                if self._stop.is_set():
                    return
                yield m

            if self._stop.is_set():
                return

            # Wait for either the poll interval or an IDLE-driven wakeup.
            await self._sleep(self._cfg.poll_interval_seconds)

    async def _sleep(self, seconds: int) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ---- IMAP internals ---------------------------------------------------

    def _fetch_batch(self) -> list[InboundEmail]:
        cfg = self._cfg
        results: list[InboundEmail] = []

        with IMAPClient(cfg.host, port=cfg.port, ssl=cfg.ssl) as client:
            client.login(cfg.username, cfg.password)
            client.select_folder(cfg.mailbox)

            since_date = (datetime.utcnow() - timedelta(days=cfg.initial_lookback_days)).date()

            # IMAP SEARCH doesn't support a native OR-over-many-terms in one
            # go, so we search per sender and take the union of UIDs.
            all_uids: set[int] = set()
            for sender in self._sender_filters:
                criteria: list = ["FROM", sender, "SINCE", since_date]
                try:
                    all_uids.update(int(u) for u in client.search(criteria))
                except Exception as exc:  # pragma: no cover
                    log.warning("IMAP search failed for sender %s: %s", sender, exc)

            if not all_uids:
                return []
            uids = sorted(all_uids)

            fetched = client.fetch(uids, ["RFC822", "UID", "FLAGS"])
            uidvalidity = client.folder_status(cfg.mailbox, [b"UIDVALIDITY"]).get(
                b"UIDVALIDITY", 0
            )

            for uid, data in fetched.items():
                raw = data.get(b"RFC822")
                if not raw:
                    continue
                msg = email.message_from_bytes(raw)
                message_id = _decode(msg.get("Message-ID"))
                fingerprint = message_id or f"uidv{uidvalidity}:uid{uid}"

                subject = _decode(msg.get("Subject"))
                from_addr = _decode(msg.get("From"))
                date_hdr = msg.get("Date")
                try:
                    parsed_date = email.utils.parsedate_to_datetime(date_hdr) if date_hdr else None
                except (TypeError, ValueError):
                    parsed_date = None

                text_body, html_body = _extract_bodies(msg)

                results.append(
                    InboundEmail(
                        fingerprint=fingerprint,
                        subject=subject,
                        from_addr=from_addr,
                        date=parsed_date,
                        text=text_body,
                        html=html_body,
                        uid=int(uid),
                    )
                )

            if cfg.mark_seen and uids:
                try:
                    client.add_flags(uids, [b"\\Seen"])
                except Exception as exc:  # pragma: no cover
                    log.warning("IMAP: could not mark messages seen: %s", exc)

        return results
