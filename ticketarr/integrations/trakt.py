"""Trakt.tv client.

Docs:
- Auth: https://docs.trakt.tv/docs/authentication-oauth
- Sync: https://docs.trakt.tv/reference (POST /sync/history + /sync/history/remove)

Uses the OAuth Device Flow so a headless service can bootstrap itself once.
Access token + refresh token are persisted to disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_BASE = "https://api.trakt.tv"


class TraktAuthError(Exception):
    pass


class TraktClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        token_path: str,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        token_expires_at: Optional[int] = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_path = Path(token_path)
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = token_expires_at or 0
        self._lock = asyncio.Lock()

        self._http = httpx.AsyncClient(
            base_url=_BASE,
            timeout=httpx.Timeout(30.0),
            headers={"Content-Type": "application/json"},
        )

        self._load_token_file()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- Token persistence ------------------------------------------------

    def _load_token_file(self) -> None:
        if self._access_token:
            return
        if not self._token_path.exists():
            return
        try:
            data = json.loads(self._token_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Trakt: cannot read token file %s: %s", self._token_path, exc)
            return
        self._access_token = data.get("access_token")
        self._refresh_token = data.get("refresh_token")
        self._expires_at = int(data.get("expires_at") or 0)

    def _save_token_file(self) -> None:
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(
            json.dumps(
                {
                    "access_token": self._access_token,
                    "refresh_token": self._refresh_token,
                    "expires_at": self._expires_at,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    # ---- Auth -------------------------------------------------------------

    async def ensure_authorized(self) -> None:
        """Ensure a valid access token exists. Runs device flow if not."""
        if self._access_token and time.time() < self._expires_at - 60:
            return
        async with self._lock:
            if self._access_token and time.time() < self._expires_at - 60:
                return
            if self._refresh_token:
                try:
                    await self._refresh()
                    return
                except TraktAuthError as exc:
                    log.warning("Trakt refresh failed, falling back to device flow: %s", exc)
            await self._device_flow()

    async def _refresh(self) -> None:
        resp = await self._http.post(
            "/oauth/token",
            json={
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code != 200:
            raise TraktAuthError(f"refresh failed: {resp.status_code} {resp.text}")
        self._store_token(resp.json())

    async def _device_flow(self) -> None:
        resp = await self._http.post(
            "/oauth/device/code",
            json={"client_id": self._client_id},
        )
        resp.raise_for_status()
        payload = resp.json()
        device_code = payload["device_code"]
        user_code = payload["user_code"]
        verification_url = payload["verification_url"]
        interval = int(payload.get("interval", 5))
        expires_in = int(payload.get("expires_in", 600))

        log.warning(
            "Trakt authorization required: go to %s and enter code %s (expires in %ds)",
            verification_url,
            user_code,
            expires_in,
        )

        deadline = time.time() + expires_in
        while time.time() < deadline:
            await asyncio.sleep(interval)
            token_resp = await self._http.post(
                "/oauth/device/token",
                json={
                    "code": device_code,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            if token_resp.status_code == 200:
                self._store_token(token_resp.json())
                log.info("Trakt: device flow authorized")
                return
            if token_resp.status_code == 400:
                continue  # pending
            if token_resp.status_code == 429:
                interval += 1
                continue
            raise TraktAuthError(
                f"device flow failed: {token_resp.status_code} {token_resp.text}"
            )
        raise TraktAuthError("device flow timed out")

    def _store_token(self, payload: dict) -> None:
        self._access_token = payload["access_token"]
        self._refresh_token = payload.get("refresh_token")
        created_at = int(payload.get("created_at") or time.time())
        expires_in = int(payload.get("expires_in") or 0)
        self._expires_at = created_at + expires_in
        self._save_token_file()

    # ---- API --------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self._client_id,
            "Authorization": f"Bearer {self._access_token}",
        }

    async def scrobble(self, tmdb_id: int, watched_at: datetime, title: str) -> bool:
        await self.ensure_authorized()
        watched = watched_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        body = {"movies": [{"watched_at": watched, "ids": {"tmdb": tmdb_id}}]}
        try:
            resp = await self._http.post("/sync/history", json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            log.error("Trakt scrobble network error for %s: %s", title, exc)
            return False
        if resp.status_code not in (200, 201):
            log.error("Trakt scrobble failed for %s: %s %s", title, resp.status_code, resp.text)
            return False
        data = resp.json()
        added = (data.get("added") or {}).get("movies", 0)
        if added:
            log.info("Trakt: scrobbled %s (tmdb=%s)", title, tmdb_id)
        else:
            log.warning("Trakt: no-op (already in history?) for %s tmdb=%s", title, tmdb_id)
        return True

    async def unscrobble(self, tmdb_id: int, watched_at: datetime | None, title: str) -> bool:
        await self.ensure_authorized()
        body = {"movies": [{"ids": {"tmdb": tmdb_id}}]}
        try:
            resp = await self._http.post(
                "/sync/history/remove", json=body, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            log.error("Trakt unscrobble network error for %s: %s", title, exc)
            return False
        if resp.status_code not in (200, 201):
            log.error(
                "Trakt unscrobble failed for %s: %s %s", title, resp.status_code, resp.text
            )
            return False
        log.info("Trakt: removed %s (tmdb=%s)", title, tmdb_id)
        return True
