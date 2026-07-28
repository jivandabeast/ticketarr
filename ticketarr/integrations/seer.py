"""Jellyseerr / Overseerr ("Seer") client.

Docs: https://api-docs.overseerr.dev/
Both Overseerr and Jellyseerr expose the same API — a single client works.

POST {base}/api/v1/request
Headers: X-Api-Key: <key>
Body:    {"mediaType": "movie", "mediaId": <tmdb_id>}
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class SeerClient:
    def __init__(self, *, base_url: str, api_key: str) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v1",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            timeout=httpx.Timeout(30.0),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request_movie(self, tmdb_id: int, title: str) -> bool:
        try:
            resp = await self._http.post(
                "/request",
                json={"mediaType": "movie", "mediaId": tmdb_id},
            )
        except httpx.HTTPError as exc:
            log.error("Seer request failed for %s: %s", title, exc)
            return False

        # 201 = created, 202 = accepted, 409 = already requested (still success for us)
        if resp.status_code in (200, 201, 202):
            log.info("Seer: requested %s (tmdb=%s)", title, tmdb_id)
            return True
        if resp.status_code == 409:
            log.info("Seer: %s already requested (tmdb=%s)", title, tmdb_id)
            return True
        log.error("Seer request failed for %s: %s %s", title, resp.status_code, resp.text)
        return False
