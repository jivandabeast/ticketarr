"""TMDB v3 client — just enough to search for a movie by title."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger(__name__)


@dataclass
class TMDBMovie:
    tmdb_id: int
    title: str
    release_date: Optional[str]  # YYYY-MM-DD
    runtime: Optional[int] = None  # minutes; only populated by get_movie()


class TMDBClient:
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, *, api_key: Optional[str] = None, bearer_token: Optional[str] = None) -> None:
        if not api_key and not bearer_token:
            raise ValueError("TMDBClient requires api_key or bearer_token")
        headers = {"Accept": "application/json"}
        params: dict[str, str] = {}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        elif api_key:
            params["api_key"] = api_key
        self._client = httpx.AsyncClient(
            base_url=self.BASE,
            headers=headers,
            params=params,
            timeout=httpx.Timeout(15.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def startup(self) -> None:
        """Verify credentials by calling TMDB's config endpoint. Raises on
        401 / 404 / connection errors so a bad key fails at boot."""
        resp = await self._client.get("/configuration")
        if resp.status_code == 401:
            raise RuntimeError(
                "TMDB rejected the configured api_key / bearer_token (HTTP 401)"
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"TMDB /configuration returned {resp.status_code}: {resp.text[:200]}"
            )
        log.info("TMDB: credentials verified")

    async def search_movie(self, title: str, year: Optional[int] = None) -> Optional[TMDBMovie]:
        params: dict[str, str] = {"query": title, "include_adult": "false"}
        if year is not None:
            params["primary_release_year"] = str(year)
        try:
            resp = await self._client.get("/search/movie", params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("TMDB search failed for %r: %s", title, exc)
            return None
        results = resp.json().get("results") or []
        if not results:
            # Retry without the year filter — AMC titles sometimes have quirks.
            if year is not None:
                return await self.search_movie(title, None)
            log.info("TMDB: no results for %r", title)
            return None

        # Prefer exact title match (case-insensitive), else the first result.
        normalized = title.strip().lower()
        best = next(
            (r for r in results if (r.get("title") or "").strip().lower() == normalized),
            results[0],
        )
        return TMDBMovie(
            tmdb_id=int(best["id"]),
            title=best.get("title") or title,
            release_date=best.get("release_date") or None,
        )

    async def get_runtime(self, tmdb_id: int) -> Optional[int]:
        """Return the movie's runtime in minutes, or None on any failure.

        Kept as a separate call (rather than folded into ``search_movie``)
        so it can be skipped when the caller doesn't need it — e.g. when
        the tracker isn't Yamtrack.
        """
        try:
            resp = await self._client.get(f"/movie/{tmdb_id}")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("TMDB runtime lookup failed for %s: %s", tmdb_id, exc)
            return None
        runtime = resp.json().get("runtime")
        if isinstance(runtime, int) and runtime > 0:
            return runtime
        return None
