"""Ombi client.

Docs: https://docs.ombi.app/

Movie request endpoint:
    POST {base}/api/v1/Request/movie
    Headers: ApiKey: <key>
    Body:    {"theMovieDbId": <tmdb_id>, "languageCode": "en"}

Note: Ombi's V2 `RequestsController` (routed at /api/v2/Requests) does NOT
expose `POST movie` — only GET routes plus `POST tv` and a couple of movie
sub-routes (`movie/advancedoptions`, `movie/collection/{id}`). Requesting a
movie is a V1 concern. Older docs and third-party scripts sometimes call
`/api/v2/Requests/movie`; that lands on ASP.NET Core's SPA fallback and
returns HTTP 500 with an "index.html not found" message.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class OmbiClient:
    def __init__(self, *, base_url: str, api_key: str, language_code: str = "en") -> None:
        self._base = base_url.rstrip("/")
        self._language = language_code
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={"ApiKey": api_key, "Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def startup(self) -> None:
        """Verify base URL + ApiKey against Ombi's /api/v1/Settings/about
        (works on every Ombi version and requires the ApiKey header)."""
        try:
            resp = await self._http.get(self._base + "/api/v1/Settings/about")
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Ombi unreachable at {self._base}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"Ombi rejected the ApiKey (HTTP {resp.status_code}). "
                "Check ombi.api_key."
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Ombi /Settings/about returned {resp.status_code}: {resp.text[:200]}"
            )
        log.info("Ombi: credentials verified")

    async def request_movie(self, tmdb_id: int, title: str) -> bool:
        body = {"theMovieDbId": tmdb_id, "languageCode": self._language}
        path = "/api/v1/Request/movie"
        try:
            resp = await self._http.post(self._base + path, json=body)
        except httpx.HTTPError as exc:
            log.error("Ombi request failed at %s for %s: %s", path, title, exc)
            return False
        if resp.status_code in (200, 201, 202):
            log.info("Ombi: requested %s (tmdb=%s)", title, tmdb_id)
            return True
        # A 500 with the SPA-fallback body means the URL didn't match any
        # controller on this Ombi server — usually because OMBI_BASE_URL is
        # wrong (missing reverse-proxy subpath, pointing at the wrong host,
        # etc). Surface that plainly.
        if resp.status_code == 500 and "SPA default page middleware" in resp.text:
            log.error(
                "Ombi request for %s hit the SPA fallback at %s%s (HTTP 500). "
                "This usually means ombi.base_url does not point at the Ombi "
                "API (check for a missing reverse-proxy subpath).",
                title, self._base, path,
            )
            return False
        log.error(
            "Ombi request failed for %s at %s: %s %s",
            title, path, resp.status_code, resp.text[:300],
        )
        return False
