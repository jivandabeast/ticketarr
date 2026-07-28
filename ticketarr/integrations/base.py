"""Common protocols for tracker and requester integrations."""

from __future__ import annotations

from typing import Protocol
from datetime import datetime


class Tracker(Protocol):
    async def scrobble(self, tmdb_id: int, watched_at: datetime, title: str) -> bool:
        """Mark a movie watched. Returns True on success."""

    async def unscrobble(self, tmdb_id: int, watched_at: datetime | None, title: str) -> bool:
        """Remove a previously-scrobbled movie. Returns True on success."""

    async def aclose(self) -> None: ...


class Requester(Protocol):
    async def request_movie(self, tmdb_id: int, title: str) -> bool:
        """Request a movie be added to the user's library. Returns True on success."""

    async def aclose(self) -> None: ...
