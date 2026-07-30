"""Ryot GraphQL client (movies only).

Docs / schema:
- Auth: Ryot v10+ authenticates GraphQL requests via a session token issued
  by the `loginUser` mutation. The extractor
  (`crates/utils/application/src/lib.rs::AuthContext`) accepts the token
  in either `Authorization: Bearer <session>` or `x-auth-token: <session>`.
  A request without a valid session yields `NO_USER_ID` from resolvers.
- Mutations: https://github.com/IgnisDa/ryot/blob/main/libs/graphql/src/backend/mutations/combined.gql

Server-side gotcha for `metadataSearch(source: TMDB)`:
  Ryot's resolver forwards the query to *its own* TMDB provider, which reads
  `MOVIES_AND_SHOWS_TMDB_ACCESS_TOKEN` (a TMDB v4 read token) from the Ryot
  container's environment. When that env var is missing/invalid, the TMDB
  HTTP call fails, Ryot's `trace_ok()` swallows the concrete error into a
  debug log, and the resolver returns the generic `"Failed to search
  metadata"`. Fix by configuring the token on the Ryot side — ticketarr's
  own TMDB key is separate and does not affect Ryot.

Flow used here:
  1. On startup: `loginUser({password: {username, password}})` → session token.
     Fall back to using ``api_key`` directly if no username/password is
     configured (advanced users who have an access-link token).
  2. Verify by calling `userDetails { __typename ... on UserDetails { id } }`
     and treating the ``UserDetailsError`` branch as a failure.
  3. Scrobble: metadataSearch → deployBulkMetadataProgressUpdate with the
     `startedAndFinishedOnDate` variant so Ryot records both a real
     ``startedOn`` (the showtime) and a real ``timestamp`` (the showtime
     plus the TMDB runtime, defaulting to 120 minutes). Same logic as the
     Yamtrack integration's start_date / end_date pair.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)


class RyotClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        default_runtime_minutes: int = 120,
    ) -> None:
        if not (api_key or (username and password)):
            raise RuntimeError(
                "Ryot: provide either ryot.api_key (an access-link token) "
                "or ryot.username + ryot.password"
            )
        self._endpoint = base_url.rstrip("/") + "/backend/graphql"
        self._username = username
        self._password = password
        self._default_runtime = default_runtime_minutes
        self._session_token: Optional[str] = api_key  # may be replaced by loginUser
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={"Content-Type": "application/json"},
        )
        if self._session_token:
            self._http.headers["Authorization"] = f"Bearer {self._session_token}"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _login(self) -> str:
        """Exchange username/password for a session token via `loginUser`."""
        assert self._username and self._password
        mutation = """
        mutation LoginUser($input: AuthUserInput!) {
          loginUser(input: $input) {
            __typename
            ... on LoginError { error }
            ... on ApiKeyResponse { apiKey }
            ... on StringIdObject { id }
          }
        }
        """
        variables = {
            "input": {
                "password": {"username": self._username, "password": self._password}
            }
        }
        try:
            resp = await self._http.post(
                self._endpoint, json={"query": mutation, "variables": variables}
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Ryot unreachable at {self._endpoint}: {exc}") from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"Ryot returned HTTP {resp.status_code} on loginUser: {resp.text[:200]}"
            )
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"Ryot loginUser failed: {payload['errors']}")
        result = (payload.get("data") or {}).get("loginUser") or {}
        typename = result.get("__typename")
        if typename == "ApiKeyResponse" and result.get("apiKey"):
            return result["apiKey"]
        if typename == "LoginError":
            raise RuntimeError(
                f"Ryot rejected credentials: {result.get('error')}. "
                "Check ryot.username and ryot.password."
            )
        if typename == "StringIdObject":
            raise RuntimeError(
                "Ryot user requires two-factor authentication, which ticketarr "
                "cannot satisfy. Disable 2FA on this user or issue an access "
                "link instead."
            )
        raise RuntimeError(f"Unexpected Ryot loginUser response: {result}")

    async def startup(self) -> None:
        """Log in (if credentials provided) and verify auth by calling userDetails."""
        if self._username and self._password:
            self._session_token = await self._login()
            self._http.headers["Authorization"] = f"Bearer {self._session_token}"

        query = """
        query { userDetails { __typename ... on UserDetails { id } ... on UserDetailsError { error } } }
        """
        try:
            resp = await self._http.post(self._endpoint, json={"query": query})
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Ryot unreachable at {self._endpoint}: {exc}") from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"Ryot returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"Ryot rejected credentials: {payload['errors']}")
        details = (payload.get("data") or {}).get("userDetails") or {}
        if details.get("__typename") == "UserDetailsError":
            raise RuntimeError(
                "Ryot session invalid. Provide ryot.username + ryot.password, "
                "or issue a fresh access link and set it as ryot.api_key."
            )
        log.info("Ryot: credentials verified")

    async def _gql(
        self,
        query: str,
        variables: dict[str, Any],
        *,
        error_hint: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        try:
            resp = await self._http.post(
                self._endpoint, json={"query": query, "variables": variables}
            )
        except httpx.HTTPError as exc:
            log.error("Ryot request failed: %s", exc)
            return None
        if resp.status_code != 200:
            log.error("Ryot HTTP %s: %s", resp.status_code, resp.text)
            return None
        payload = resp.json()
        if payload.get("errors"):
            if error_hint:
                log.error(
                    "Ryot GraphQL errors: %s. %s", payload["errors"], error_hint
                )
            else:
                log.error("Ryot GraphQL errors: %s", payload["errors"])
            return None
        return payload.get("data")

    async def _find_metadata_id(self, title: str, tmdb_id: int) -> Optional[str]:
        query = """
        query MetadataSearch($input: MetadataSearchInput!) {
          metadataSearch(input: $input) {
            response { items }
          }
        }
        """
        variables = {
            "input": {
                "lot": "MOVIE",
                "source": "TMDB",
                "search": {"query": title, "page": 1, "take": 10},
            }
        }
        # Ryot's metadataSearch resolver calls its own TMDB provider using the
        # server-side MOVIES_AND_SHOWS_TMDB_ACCESS_TOKEN env var. If that token
        # is missing/invalid, the resolver swallows the underlying error and
        # returns the generic "Failed to search metadata". Point operators at
        # the real fix instead of leaving the message opaque.
        hint = (
            "If the message is 'Failed to search metadata', Ryot's own TMDB "
            "provider failed — set MOVIES_AND_SHOWS_TMDB_ACCESS_TOKEN "
            "(a TMDB v4 read access token) on the Ryot server and restart it."
        )
        data = await self._gql(query, variables, error_hint=hint)
        if not data:
            return None
        items = (
            (data.get("metadataSearch") or {}).get("response", {}).get("items") or []
        )
        if not items:
            return None
        # items are strings (metadataIds) in Ryot's current schema.
        return items[0] if isinstance(items[0], str) else items[0].get("identifier")

    async def scrobble(
        self,
        tmdb_id: int,
        watched_at: datetime,
        title: str,
        *,
        runtime_minutes: Optional[int] = None,
        **_kwargs,
    ) -> bool:
        metadata_id = await self._find_metadata_id(title, tmdb_id)
        if not metadata_id:
            log.warning("Ryot: could not resolve %s to a metadataId", title)
            return False

        # Mirror the yamtrack scrobble: use the showtime as the "started on"
        # and showtime + runtime as the "finished on". Fall back to the
        # configured default duration when TMDB doesn't know the runtime, so
        # start != end even for unknown-runtime films.
        duration = (
            runtime_minutes
            if runtime_minutes and runtime_minutes > 0
            else self._default_runtime
        )
        started_on = _to_utc_iso(watched_at)
        finished_on = _to_utc_iso(watched_at + timedelta(minutes=duration))
        mutation = """
        mutation DeployBulkMetadataProgressUpdate($input: [MetadataProgressUpdateInput!]!) {
          deployBulkMetadataProgressUpdate(input: $input)
        }
        """
        variables = {
            "input": [
                {
                    "metadataId": metadata_id,
                    "change": {
                        "createNewCompleted": {
                            "startedAndFinishedOnDate": {
                                "startedOn": started_on,
                                "timestamp": finished_on,
                            }
                        }
                    },
                }
            ]
        }
        data = await self._gql(mutation, variables)
        if data is None:
            return False
        log.info(
            "Ryot: scrobbled %s (metadataId=%s, started=%s, finished=%s)",
            title,
            metadata_id,
            started_on,
            finished_on,
        )
        return True

    async def unscrobble(self, tmdb_id: int, watched_at: datetime | None, title: str, **_kwargs) -> bool:
        metadata_id = await self._find_metadata_id(title, tmdb_id)
        if not metadata_id:
            log.warning("Ryot: could not resolve %s to a metadataId (unscrobble)", title)
            return False

        history_query = """
        query UserMetadataDetails($metadataId: String!) {
          userMetadataDetails(metadataId: $metadataId) { history { id } }
        }
        """
        data = await self._gql(history_query, {"metadataId": metadata_id})
        if not data:
            return False
        history = (data.get("userMetadataDetails") or {}).get("history") or []
        if not history:
            log.info("Ryot: no history entry to remove for %s", title)
            return True

        latest_seen_id = history[0]["id"]
        mutation = """
        mutation DeleteSeenItem($seenId: String!) {
          deleteSeenItem(seenId: $seenId) { id }
        }
        """
        result = await self._gql(mutation, {"seenId": latest_seen_id})
        if result is None:
            return False
        log.info("Ryot: removed %s (seenId=%s)", title, latest_seen_id)
        return True


def _to_utc_iso(when: datetime) -> str:
    """Serialise a datetime the way Ryot's async_graphql `DateTime` scalar
    accepts: an ISO-8601 UTC string terminated with 'Z'. Naive inputs are
    assumed to already be UTC (that's what our parsers produce)."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
