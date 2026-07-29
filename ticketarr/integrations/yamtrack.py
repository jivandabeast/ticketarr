"""Yamtrack client.

Yamtrack has no official REST API for creating watched-movie entries. Its
public integration surface is a Jellyfin-style webhook receiver that sets
``watched_at`` server-side (``timezone.now()``), which makes it useless for
backdating a movie showtime hours or days after we receive the ticket
email.

To get real timestamps into Yamtrack we drive the same Django form
handler that Yamtrack's own "edit / add media" modal posts to:
``POST /media_save`` with an empty ``instance_id`` creates a brand-new
Movie row with the ``start_date`` / ``end_date`` we specify. This is
technically an **internal** endpoint — the contract could change in any
Yamtrack release — but it's currently the only way to backdate.

Design decisions:

* **Always-create** rather than update-in-place. Regal Unlimited / AMC
  A-List members rewatch things frequently; one row per showing keeps
  each ticket's start/end distinct and never clobbers user-edited
  score/notes.
* Session-cookie auth via django-allauth's ``/accounts/login/`` (there is
  no token endpoint that grants access to ``/media_save``).
* After creating a row we look it up via ``track_modal`` — the
  ``Media`` model's default ordering is ``["user", "item", "-created_at"]``
  so the row we just made is the "newest" one, and it's the one
  ``track_modal`` returns when called without an explicit ``instance_id``.
* ``end_date`` = ``start_date + runtime`` (from TMDB), falling back to
  a fixed 120-minute duration if the runtime is unknown.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

log = logging.getLogger(__name__)


# `<input ... name="instance_id" value="21455" ...>` in track_modal HTML.
_INSTANCE_ID_RE = re.compile(
    r"""name=["']instance_id["'][^>]*value=["'](?P<id>\d+)["']"""
    r"""|"""
    r"""value=["'](?P<id2>\d+)["'][^>]*name=["']instance_id["']""",
    re.IGNORECASE,
)


class YamtrackClient:
    """Talks to Yamtrack's Django form endpoints via a session cookie.

    A single ``httpx.AsyncClient`` holds the session cookie for the life
    of the object; ``startup()`` performs the login. All writes send an
    ``X-CSRFToken`` header sourced from the ``csrftoken`` cookie.
    """

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        default_runtime_minutes: int = 120,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._default_runtime = default_runtime_minutes
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
            headers={"User-Agent": "ticketarr/1.0"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- auth -------------------------------------------------------------

    async def startup(self) -> None:
        """Log in and confirm the session works.

        Fails with ``RuntimeError`` on any misconfiguration so the
        orchestrator can bail at boot rather than silently spamming
        failed writes later.
        """
        await self._login()
        # A brief sanity call: hit /accounts/login/ again — if we're still
        # authenticated Yamtrack redirects to the home page (302), and if
        # somehow the session cookie didn't stick we get the login form
        # (200 with a fresh CSRF token). Anything else means the server is
        # broken.
        resp = await self._http.get("/")
        if resp.status_code not in (200, 302):
            raise RuntimeError(
                f"Yamtrack home check returned HTTP {resp.status_code}"
            )
        # 200 could be the anonymous landing page — verify we see a
        # "logout" link somewhere. django-allauth's default nav always
        # includes a logout form.
        body = resp.text.lower()
        if "logout" not in body and "sign out" not in body:
            raise RuntimeError(
                "Yamtrack login appeared to succeed but the home page is "
                "anonymous; check the configured username/password."
            )
        log.info("Yamtrack: session authenticated as %s", self._username)

    async def _login(self) -> None:
        try:
            get = await self._http.get("/accounts/login/")
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Yamtrack unreachable at {self._base_url}: {exc}") from exc
        if get.status_code >= 400:
            raise RuntimeError(
                f"Yamtrack /accounts/login/ returned HTTP {get.status_code}"
            )
        csrf = self._csrf_from_html(get.text) or self._csrf_from_cookies()
        if not csrf:
            raise RuntimeError(
                "Yamtrack: could not find a CSRF token on the login page"
            )
        try:
            post = await self._http.post(
                "/accounts/login/",
                data={
                    "login": self._username,
                    "password": self._password,
                    "csrfmiddlewaretoken": csrf,
                },
                headers={"Referer": f"{self._base_url}/accounts/login/"},
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Yamtrack login POST failed: {exc}") from exc
        # A successful allauth login is a 302 away from /accounts/login/.
        # A failed one re-renders the form with 200 + an error message.
        if post.status_code == 302:
            return
        if post.status_code == 200 and "logout" not in post.text.lower():
            raise RuntimeError(
                "Yamtrack rejected the configured username/password"
            )
        if post.status_code >= 400:
            raise RuntimeError(
                f"Yamtrack login returned HTTP {post.status_code}: "
                f"{post.text[:200]}"
            )

    # ---- Tracker protocol ------------------------------------------------

    async def scrobble(
        self,
        tmdb_id: int,
        watched_at: datetime,
        title: str,
        *,
        runtime_minutes: Optional[int] = None,
    ) -> Optional[int]:
        """Create a new Yamtrack Movie row and return its ``instance_id``.

        Returns ``None`` if the write failed. Callers should persist the
        returned id in ticketarr's state so a later cancellation can
        target the exact row.
        """
        start = self._as_local_naive(watched_at)
        duration = runtime_minutes if runtime_minutes and runtime_minutes > 0 else self._default_runtime
        end = start + timedelta(minutes=duration)
        form = {
            "media_type": "movie",
            "source": "tmdb",
            "media_id": str(tmdb_id),
            "status": "Completed",
            "score": "",
            "start_date": start.strftime("%Y-%m-%dT%H:%M"),
            "end_date": end.strftime("%Y-%m-%dT%H:%M"),
            "notes": "",
            # instance_id omitted → creates a new row
        }
        try:
            resp = await self._post_form("/media_save", form)
        except httpx.HTTPError as exc:
            log.error("Yamtrack scrobble request failed for %s: %s", title, exc)
            return None
        if resp.status_code >= 400:
            log.error(
                "Yamtrack /media_save failed for %s: HTTP %s %s",
                title,
                resp.status_code,
                resp.text[:200],
            )
            return None

        instance_id = await self._latest_instance_id(tmdb_id)
        if instance_id is None:
            log.warning(
                "Yamtrack: created row for %s but could not resolve its instance_id",
                title,
            )
        else:
            log.info(
                "Yamtrack: scrobbled %s (tmdb=%s) as instance=%s",
                title,
                tmdb_id,
                instance_id,
            )
        return instance_id

    async def unscrobble(
        self,
        tmdb_id: int,
        watched_at: datetime | None,
        title: str,
        *,
        instance_id: Optional[int] = None,
    ) -> bool:
        """Delete a previously-scrobbled row.

        Preferred path: caller supplies the ``instance_id`` returned by
        ``scrobble()``. If missing (e.g. state predates OCR support), we
        fall back to deleting the newest row for that ``tmdb_id``.
        """
        if instance_id is None:
            instance_id = await self._latest_instance_id(tmdb_id)
        if instance_id is None:
            log.warning(
                "Yamtrack: nothing to unscrobble for %s (tmdb=%s)",
                title,
                tmdb_id,
            )
            return False
        try:
            resp = await self._post_form(
                "/media_delete",
                {"instance_id": str(instance_id), "media_type": "movie"},
            )
        except httpx.HTTPError as exc:
            log.error("Yamtrack unscrobble failed for %s: %s", title, exc)
            return False
        if resp.status_code >= 400:
            log.error(
                "Yamtrack /media_delete failed for %s (instance=%s): HTTP %s %s",
                title,
                instance_id,
                resp.status_code,
                resp.text[:200],
            )
            return False
        log.info(
            "Yamtrack: removed %s (tmdb=%s, instance=%s)",
            title,
            tmdb_id,
            instance_id,
        )
        return True

    # ---- helpers ---------------------------------------------------------

    async def _post_form(self, path: str, data: dict[str, str]) -> httpx.Response:
        csrf = self._csrf_from_cookies()
        if not csrf:
            # Cookie may have expired mid-run; re-login and try again.
            await self._login()
            csrf = self._csrf_from_cookies()
        payload = dict(data)
        if csrf:
            payload["csrfmiddlewaretoken"] = csrf
        return await self._http.post(
            path,
            data=payload,
            headers={
                "X-CSRFToken": csrf or "",
                "Referer": f"{self._base_url}/",
            },
        )

    async def _latest_instance_id(self, tmdb_id: int) -> Optional[int]:
        """Ask Yamtrack for the newest Movie row we have for this tmdb id.

        We use ``track_modal`` because it renders a form pre-populated
        with the ``instance_id`` of ``filter_media(...).first()``. The
        Media model's default ordering is ``["user","item","-created_at"]``
        so ``first()`` is the most-recently-created row — i.e. the one
        our preceding ``/media_save`` just made.
        """
        try:
            resp = await self._http.get(
                f"/track_modal/tmdb/movie/{tmdb_id}",
                params={"return_url": "/"},
            )
        except httpx.HTTPError as exc:
            log.warning("Yamtrack track_modal fetch failed for tmdb=%s: %s", tmdb_id, exc)
            return None
        if resp.status_code >= 400:
            log.warning(
                "Yamtrack track_modal for tmdb=%s returned HTTP %s",
                tmdb_id,
                resp.status_code,
            )
            return None
        match = _INSTANCE_ID_RE.search(resp.text)
        if not match:
            return None
        raw = match.group("id") or match.group("id2")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _csrf_from_cookies(self) -> Optional[str]:
        return self._http.cookies.get("csrftoken")

    @staticmethod
    def _csrf_from_html(html: str) -> Optional[str]:
        match = re.search(
            r"""name=["']csrfmiddlewaretoken["'][^>]*value=["']([^"']+)["']""",
            html,
        )
        return match.group(1) if match else None

    @staticmethod
    def _as_local_naive(when: datetime) -> datetime:
        """Yamtrack stores datetimes as naive local wall-clock strings.

        The ``datetime-local`` form widget doesn't accept timezone info;
        it's parsed with Django's active timezone. We strip the tz to
        avoid the browser-would-have-converted-it-to-UTC surprise: the
        showtime we captured is already the local wall-clock time of the
        theater, and that's what the user wants to see in Yamtrack.
        """
        if when.tzinfo is None:
            return when
        # If it's UTC (which is what we usually get from parsers), keep
        # the wall-clock digits — the parser already localized them to
        # the theater's timezone before UTC-stamping if it had that info.
        return when.astimezone(timezone.utc).replace(tzinfo=None)
