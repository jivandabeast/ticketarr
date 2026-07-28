"""Common protocols for tracker and requester integrations."""

from __future__ import annotations

from typing import Protocol
from datetime import datetime


class Tracker(Protocol):
    async def startup(self) -> None:
        """Optional eager initialization (e.g. Trakt device-flow auth).

        Called once at process start so credential / auth problems surface
        immediately instead of on the first email. Default no-op is fine for
        API-key based trackers.
        """

    async def scrobble(self, tmdb_id: int, watched_at: datetime, title: str) -> bool:
        """Mark a movie watched. Returns True on success."""

    async def unscrobble(self, tmdb_id: int, watched_at: datetime | None, title: str) -> bool:
        """Remove a previously-scrobbled movie. Returns True on success."""

    async def aclose(self) -> None: ...


class Requester(Protocol):
    async def startup(self) -> None:
        """Optional eager credential / connectivity check. Called once at
        boot; raise on failure so misconfiguration is obvious."""

    async def request_movie(self, tmdb_id: int, title: str) -> bool:
        """Request a movie be added to the user's library. Returns True on success."""

    async def aclose(self) -> None: ...
