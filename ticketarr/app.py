"""Application orchestrator: wires config → IMAP → parser → integrations."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .config import Config
from .imap_monitor import IMAPMonitor, InboundEmail
from .integrations.base import Requester, Tracker
from .parsers import ParsedEmail, default_sender_filters, parse_email
from .state import OrderRecord, StateStore
from .tmdb import TMDBClient, TMDBMovie

log = logging.getLogger(__name__)


def _build_tracker(cfg: Config) -> Optional[Tracker]:
    provider = cfg.tracker.provider
    if provider == "trakt":
        from .integrations.trakt import TraktClient

        if not (cfg.trakt.client_id and cfg.trakt.client_secret):
            raise RuntimeError("Trakt selected but client_id/client_secret missing")
        return TraktClient(
            client_id=cfg.trakt.client_id,
            client_secret=cfg.trakt.client_secret,
            token_path=cfg.trakt.token_path,
            access_token=cfg.trakt.access_token,
            refresh_token=cfg.trakt.refresh_token,
            token_expires_at=cfg.trakt.token_expires_at,
        )
    if provider == "ryot":
        from .integrations.ryot import RyotClient

        if not cfg.ryot.base_url:
            raise RuntimeError("Ryot selected but base_url missing")
        if not (cfg.ryot.api_key or (cfg.ryot.username and cfg.ryot.password)):
            raise RuntimeError(
                "Ryot selected but no credentials given: set either "
                "ryot.username + ryot.password, or ryot.api_key"
            )
        return RyotClient(
            base_url=cfg.ryot.base_url,
            api_key=cfg.ryot.api_key,
            username=cfg.ryot.username,
            password=cfg.ryot.password,
            default_runtime_minutes=cfg.ryot.default_runtime_minutes,
        )
    if provider == "yamtrack":
        from .integrations.yamtrack import YamtrackClient

        if not (cfg.yamtrack.base_url and cfg.yamtrack.username and cfg.yamtrack.password):
            raise RuntimeError(
                "Yamtrack selected but base_url/username/password missing"
            )
        return YamtrackClient(
            base_url=cfg.yamtrack.base_url,
            username=cfg.yamtrack.username,
            password=cfg.yamtrack.password,
        )
    return None


def _build_requester(cfg: Config) -> Optional[Requester]:
    provider = cfg.requester.provider
    if provider == "seerr":
        from .integrations.seerr import SeerrClient

        if not (cfg.seerr.base_url and cfg.seerr.api_key):
            raise RuntimeError("Seerr selected but base_url/api_key missing")
        return SeerrClient(
            base_url=cfg.seerr.base_url,
            api_key=cfg.seerr.api_key,
            request_4k=cfg.seerr.request_4k,
        )
    if provider == "ombi":
        from .integrations.ombi import OmbiClient

        if not (cfg.ombi.base_url and cfg.ombi.api_key):
            raise RuntimeError("Ombi selected but base_url/api_key missing")
        return OmbiClient(
            base_url=cfg.ombi.base_url,
            api_key=cfg.ombi.api_key,
            language_code=cfg.ombi.language_code,
        )
    return None


class Application:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state = StateStore(cfg.state_path)
        self.state.load()
        self.tmdb = TMDBClient(
            api_key=cfg.tmdb.api_key,
            bearer_token=cfg.tmdb.bearer_token,
        )
        self.tracker = _build_tracker(cfg)
        self.requester = _build_requester(cfg)
        senders = cfg.imap.sender_filters or default_sender_filters()
        if not senders:
            raise RuntimeError(
                "No IMAP sender_filters configured and no parsers registered"
            )
        self.monitor = IMAPMonitor(cfg.imap, senders)
        self._stopping = False
        self._ready = False
        self._verified: dict[str, str] = {}  # component -> "ok" | error message

        self._fastapi = FastAPI(title="ticketarr", docs_url=None, redoc_url=None, openapi_url=None)
        self._fastapi.get("/healthz")(self._healthz)
        self._server: Optional[uvicorn.Server] = None

    # ---- healthcheck ------------------------------------------------------

    async def _healthz(self) -> JSONResponse:
        """503 until every configured integration has verified.

        Once ready, returns {"status": "ok", "components": {...}}. If a
        component ever transitions to a failed state at runtime we surface
        that here too.
        """
        if not self._ready:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "starting",
                    "components": self._verified or {"boot": "pending"},
                },
            )
        if any(v != "ok" for v in self._verified.values()):
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "components": self._verified},
            )
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "components": self._verified},
        )

    # ---- lifecycle --------------------------------------------------------

    async def run(self) -> None:
        server_task = asyncio.create_task(self._run_healthcheck())
        sweeper_task = asyncio.create_task(self._flush_pending_scrobbles_forever())
        try:
            await self._process_forever()
        finally:
            self._stopping = True
            if self._server is not None:
                self._server.should_exit = True
            await asyncio.gather(server_task, sweeper_task, return_exceptions=True)
            await self._close()

    async def stop(self) -> None:
        self._stopping = True
        self.monitor.stop()
        if self._server is not None:
            self._server.should_exit = True

    async def _close(self) -> None:
        await self.tmdb.aclose()
        if self.tracker is not None:
            await self.tracker.aclose()
        if self.requester is not None:
            await self.requester.aclose()
        await self.state.save()

    async def _run_healthcheck(self) -> None:
        config = uvicorn.Config(
            self._fastapi,
            host="0.0.0.0",
            port=self.cfg.healthcheck_port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        try:
            await self._server.serve()
        except Exception as exc:  # pragma: no cover
            log.warning("Healthcheck server exited: %s", exc)

    # ---- startup verification --------------------------------------------

    async def _verify_all(self) -> None:
        """Verify every configured integration before entering the main loop.

        Failures here are FATAL — we log the offending component and re-raise
        so docker/systemd restart policies can retry with a clean slate. The
        /healthz endpoint stays 503 until every component reports "ok".
        """
        components: list[tuple[str, object]] = [
            ("imap", self.monitor),
            ("tmdb", self.tmdb),
        ]
        if self.tracker is not None:
            components.append((f"tracker:{self.cfg.tracker.provider}", self.tracker))
        if self.requester is not None:
            components.append((f"requester:{self.cfg.requester.provider}", self.requester))

        for name, component in components:
            startup = getattr(component, "startup", None)
            if startup is None:
                self._verified[name] = "ok"
                continue
            try:
                await startup()
            except Exception as exc:
                self._verified[name] = f"error: {exc}"
                log.error("%s verification failed: %s", name, exc)
                raise
            self._verified[name] = "ok"

        self._ready = True
        log.info("All integrations verified: %s", ", ".join(self._verified))

    # ---- main loop --------------------------------------------------------

    async def _process_forever(self) -> None:
        log.info(
            "Starting: tracker=%s requester=%s inbox=%s@%s",
            self.cfg.tracker.provider,
            self.cfg.requester.provider,
            self.cfg.imap.username,
            self.cfg.imap.host,
        )
        await self._verify_all()
        # Catch up on any scrobbles whose showtime passed while we were down
        # before we start ingesting new email. Doing this eagerly means the
        # first IMAP tick doesn't have to race the sweeper.
        await self._flush_pending_scrobbles()
        async for msg in self.monitor.stream():
            if self._stopping:
                break
            try:
                await self._handle_message(msg)
            except Exception:  # pragma: no cover - defensive
                log.exception("Failed to handle message %s", msg.fingerprint)

    async def _flush_pending_scrobbles_forever(self) -> None:
        """Periodically dispatch scrobbles whose showtime has passed.

        Setting ``pending_scrobble_check_interval_seconds`` to 0 disables the
        loop (the on-boot flush still runs, and any new email will still
        redrive its own record inline)."""
        interval = self.cfg.pending_scrobble_check_interval_seconds
        if interval <= 0:
            log.info("Pending-scrobble sweeper disabled (interval=0)")
            return
        while not self._stopping:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:  # pragma: no cover
                break
            if self._stopping:
                break
            if not self._ready:
                # Startup verification still running; skip this tick.
                continue
            try:
                await self._flush_pending_scrobbles()
            except Exception:  # pragma: no cover - defensive
                log.exception("Pending-scrobble sweep failed")

    async def _flush_pending_scrobbles(self) -> None:
        """Scan every order and dispatch any whose showtime has passed."""
        if self.tracker is None:
            return
        due: list[OrderRecord] = []
        for record in self.state.state.orders.values():
            if record.scrobbled:
                continue
            watched_at = _parse_iso(record.watched_at) if record.watched_at else None
            if watched_at is None or not _showtime_has_passed(watched_at):
                continue
            due.append(record)
        if not due:
            return
        log.info("Flushing %d pending scrobble(s)", len(due))
        changed = False
        for record in due:
            before = record.scrobbled
            await self._dispatch_scrobble(record)
            if record.scrobbled != before or record.tracker_ids:
                changed = True
        if changed:
            await self.state.save()

    async def _handle_message(self, msg: InboundEmail) -> None:
        if self.state.already_processed(msg.fingerprint):
            return

        parsed = parse_email(
            msg.subject, msg.html, msg.text,
            from_addr=msg.from_addr,
            images=msg.images,
        )

        if parsed.kind == "other" or not parsed.ok:
            if parsed.skip_reason:
                log.info("Skipping (%s): %s", parsed.skip_reason, msg.subject)
            elif not parsed.ok:
                log.info("Could not parse email %r: %s", msg.subject, parsed.error)
            self.state.mark_processed(msg.fingerprint)
            await self.state.save()
            return

        if parsed.kind == "reservation":
            await self._handle_reservation(parsed)
        elif parsed.kind == "cancellation":
            await self._handle_cancellation(parsed)
        else:
            # thank_you is informational; ignore for now.
            log.debug("Ignoring thank-you email for %s", parsed.title)

        self.state.mark_processed(msg.fingerprint)
        await self.state.save()

    # ---- handlers ---------------------------------------------------------

    async def _handle_reservation(self, parsed: ParsedEmail) -> None:
        assert parsed.title and parsed.order_number and parsed.showtime
        log.info(
            "Reservation: %s @ %s (showtime=%s, order=%s)",
            parsed.title,
            parsed.theater_name,
            parsed.showtime.isoformat(),
            parsed.order_number,
        )

        movie = await self._lookup_tmdb(parsed.title, parsed.showtime)
        if movie is None:
            # Record with best-effort info so a later cancellation can still be cleaned.
            self.state.record_order(
                OrderRecord(
                    order_number=parsed.order_number,
                    tmdb_id=None,
                    title=parsed.title,
                    watched_at=parsed.showtime.isoformat(),
                    theater_name=parsed.theater_name,
                    scrobbled=False,
                )
            )
            return

        watched_at = parsed.showtime

        # Requests always go out immediately — the whole point of a request
        # is to give the ARRs time to grab the movie before the user gets
        # home, so it makes no sense to defer them to the showtime like the
        # scrobble.
        if self.requester is not None:
            await self.requester.request_movie(movie.tmdb_id, movie.title)

        # Base record. If the showtime is already in the past (uncommon but
        # possible for older emails picked up on a fresh install) we scrobble
        # inline. Otherwise we persist ``scrobbled=False`` and let the
        # sweeper pick it up when the showtime has actually passed — Trakt
        # rejects future ``watched_at`` values, and marking future shows as
        # watched is semantically wrong on the other trackers too.
        record = OrderRecord(
            order_number=parsed.order_number,
            tmdb_id=movie.tmdb_id,
            title=movie.title,
            watched_at=watched_at.isoformat(),
            theater_name=parsed.theater_name,
            scrobbled=False,
        )
        if _showtime_has_passed(watched_at):
            await self._dispatch_scrobble(record)
        else:
            log.info(
                "Deferring scrobble for %s until showtime %s has passed",
                movie.title,
                watched_at.isoformat(),
            )
        self.state.record_order(record)

    async def _dispatch_scrobble(self, record: OrderRecord) -> None:
        """Call ``tracker.scrobble`` and mutate ``record`` in place with the
        results (``scrobbled=True`` and, for Yamtrack, the new instance_id).
        Shared by the inline (past-showtime) and the sweeper (deferred) paths
        so the runtime lookup + instance_id capture live in exactly one
        place."""
        if self.tracker is None or record.tmdb_id is None:
            record.scrobbled = True  # nothing to do; don't keep re-checking
            return
        watched_at = _parse_iso(record.watched_at) if record.watched_at else None
        if watched_at is None:
            log.warning(
                "Cannot scrobble order %s: watched_at is missing/invalid",
                record.order_number,
            )
            record.scrobbled = True
            return

        # Yamtrack and Ryot both use the movie's runtime to compute an
        # end/finished-on timestamp. Trakt does not care about duration.
        # Only pay for the extra TMDB call when the tracker will actually
        # use it.
        scrobble_kwargs: dict[str, object] = {}
        if self.cfg.tracker.provider in ("yamtrack", "ryot"):
            runtime = await self.tmdb.get_runtime(record.tmdb_id)
            if runtime is not None:
                scrobble_kwargs["runtime_minutes"] = runtime

        title = record.title or f"tmdb:{record.tmdb_id}"
        try:
            result = await self.tracker.scrobble(
                record.tmdb_id, watched_at, title, **scrobble_kwargs
            )
        except Exception:
            log.exception(
                "Scrobble failed for order %s (%s); will retry on next sweep",
                record.order_number,
                title,
            )
            return

        # Yamtrack's scrobble() returns the newly-created instance_id
        # (or None on failure). Everything else returns a bool.
        if self.cfg.tracker.provider == "yamtrack":
            if isinstance(result, int):
                record.tracker_ids["yamtrack"] = str(result)
                record.scrobbled = True
            else:
                log.warning(
                    "Yamtrack scrobble for order %s did not return an "
                    "instance_id; will retry on next sweep",
                    record.order_number,
                )
        else:
            if result:
                record.scrobbled = True
            else:
                log.warning(
                    "Scrobble for order %s (%s) returned False; will retry "
                    "on next sweep",
                    record.order_number,
                    title,
                )

    async def _handle_cancellation(self, parsed: ParsedEmail) -> None:
        assert parsed.order_number
        log.info("Cancellation for order %s", parsed.order_number)
        record = self.state.pop_order(parsed.order_number)

        # If the scrobble never actually went out (future-showtime deferral)
        # popping the record is enough — nothing exists on the tracker to
        # remove, and we already skipped every third-party call for it.
        if record is not None and not record.scrobbled:
            log.info(
                "Cancellation %s: reservation was still pending; dropped "
                "before scrobbling",
                parsed.order_number,
            )
            return

        tmdb_id: Optional[int] = record.tmdb_id if record else None
        title = (record.title if record else parsed.title) or parsed.title
        watched_at = _parse_iso(record.watched_at) if record and record.watched_at else parsed.showtime

        if tmdb_id is None and title:
            movie = await self._lookup_tmdb(title, watched_at)
            if movie is not None:
                tmdb_id = movie.tmdb_id
                title = movie.title

        if tmdb_id is None:
            log.warning(
                "Cancellation %s: no TMDB id available; nothing to remove",
                parsed.order_number,
            )
            return

        if self.tracker is not None:
            unscrobble_kwargs: dict[str, object] = {}
            if self.cfg.tracker.provider == "yamtrack" and record is not None:
                raw_iid = record.tracker_ids.get("yamtrack")
                if raw_iid:
                    try:
                        unscrobble_kwargs["instance_id"] = int(raw_iid)
                    except ValueError:
                        pass
            await self.tracker.unscrobble(
                tmdb_id, watched_at, title or f"tmdb:{tmdb_id}", **unscrobble_kwargs
            )

    async def _lookup_tmdb(self, title: str, showtime: Optional[datetime]) -> Optional[TMDBMovie]:
        year = showtime.year if showtime else None
        return await self.tmdb.search_movie(title, year=year)


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _showtime_has_passed(when: datetime) -> bool:
    """True when ``when`` is now or in the past.

    Naive datetimes are treated as UTC, matching what our parsers produce.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= datetime.now(timezone.utc)
