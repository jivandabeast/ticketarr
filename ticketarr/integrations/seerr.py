"""Jellyseerr / Overseerr ("Seerr") client.

Docs: https://api-docs.overseerr.dev/
Both Overseerr and Jellyseerr expose the same API — a single client works.

POST {base}/api/v1/request
Headers: X-Api-Key: <key>
Body:    {"mediaType": "movie", "mediaId": <tmdb_id>, "is4k": <bool>}
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class SeerrClient:
    def __init__(self, *, base_url: str, api_key: str, request_4k: bool = False) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v1",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            timeout=httpx.Timeout(30.0),
        )
        self._request_4k = request_4k

    async def aclose(self) -> None:
        await self._http.aclose()

    async def startup(self) -> None:
        """Verify base URL + API key against Jellyseerr/Overseerr's
        /auth/me endpoint (requires an authenticated session/API key)."""
        try:
            resp = await self._http.get("/auth/me")
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Seerr unreachable: {exc}") from exc
        if resp.status_code == 403 or resp.status_code == 401:
            raise RuntimeError(
                f"Seerr rejected the API key (HTTP {resp.status_code}). "
                "Check seerr.api_key."
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Seerr /auth/me returned {resp.status_code}: {resp.text[:200]}"
            )
        log.info("Seerr: credentials verified")

    async def request_movie(self, tmdb_id: int, title: str) -> bool:
        payload: dict[str, object] = {"mediaType": "movie", "mediaId": tmdb_id}
        if self._request_4k:
            payload["is4k"] = True
        try:
            resp = await self._http.post("/request", json=payload)
        except httpx.HTTPError as exc:
            log.error("Seerr request failed for %s: %s", title, exc)
            return False

        quality = "4K" if self._request_4k else "standard"
        # 201 = created, 202 = accepted, 409 = already requested (still success for us)
        if resp.status_code in (200, 201, 202):
            log.info("Seerr: requested %s [%s] (tmdb=%s)", title, quality, tmdb_id)
            return True
        if resp.status_code == 409:
            log.info("Seerr: %s [%s] already requested (tmdb=%s)", title, quality, tmdb_id)
            return True
        log.error("Seerr request failed for %s: %s %s", title, resp.status_code, resp.text)
        return False
