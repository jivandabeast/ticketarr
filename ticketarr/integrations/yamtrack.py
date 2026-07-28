"""Yamtrack client.

Yamtrack currently has no first-class REST API; the closest supported entry
point is its Jellyfin-style webhook receiver at
``POST /webhook/jellyfin/<user_token>``. The processor uses
``Item.ProviderIds.Tmdb`` and treats ``Event=MarkPlayed`` as "watched",
``Event=MarkUnplayed`` as "unwatched".

Notes:
- Yamtrack sets watched_at to ``timezone.now()`` server-side (the caller's
  timestamp is not honored).
- The user must enable "Sync mark played" in Yamtrack → Integrations for
  ``MarkPlayed``/``MarkUnplayed`` to take effect.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)


class YamtrackClient:
    def __init__(self, *, base_url: str, webhook_token: str) -> None:
        self._url = f"{base_url.rstrip('/')}/webhook/jellyfin/{webhook_token}"
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def startup(self) -> None:
        """Confirm the webhook URL is reachable and the token is valid.

        Yamtrack has no ping endpoint, but hitting the webhook with a
        malformed payload gives us a signal: reachable + accepted-token
        returns 400/422, wrong token returns 404, wrong host raises."""
        try:
            resp = await self._http.post(self._url, json={"Event": "ticketarr.ping"})
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Yamtrack unreachable at {self._url}: {exc}") from exc
        if resp.status_code == 404:
            raise RuntimeError(
                "Yamtrack rejected the webhook token (HTTP 404). "
                "Check yamtrack.webhook_token and the base_url."
            )
        # Any other status (200/204/400/422/500) proves the endpoint exists and
        # the token is accepted. Yamtrack's exact behavior varies by version.
        log.info("Yamtrack: webhook reachable")

    def _payload(self, tmdb_id: int, title: str, event: str) -> dict[str, Any]:
        return {
            "Event": event,
            "Item": {
                "Type": "Movie",
                "Name": title,
                "ProviderIds": {"Tmdb": str(tmdb_id)},
                "UserData": {"Played": event in ("Play", "MarkPlayed")},
            },
        }

    async def _post(self, tmdb_id: int, title: str, event: str) -> bool:
        try:
            resp = await self._http.post(self._url, json=self._payload(tmdb_id, title, event))
        except httpx.HTTPError as exc:
            log.error("Yamtrack request failed: %s", exc)
            return False
        if resp.status_code >= 300:
            log.error(
                "Yamtrack %s failed for %s: %s %s",
                event,
                title,
                resp.status_code,
                resp.text,
            )
            return False
        log.info("Yamtrack: %s %s (tmdb=%s)", event, title, tmdb_id)
        return True

    async def scrobble(self, tmdb_id: int, watched_at: datetime, title: str) -> bool:
        return await self._post(tmdb_id, title, "MarkPlayed")

    async def unscrobble(self, tmdb_id: int, watched_at: datetime | None, title: str) -> bool:
        return await self._post(tmdb_id, title, "MarkUnplayed")
