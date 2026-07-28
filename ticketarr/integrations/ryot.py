"""Ryot GraphQL client (movies only).

Docs / schema:
- Auth: https://docs.ryot.io/guides/authentication
- Mutations: https://github.com/IgnisDa/ryot/blob/main/libs/graphql/src/backend/mutations/combined.gql

Flow used here:
  1. metadataSearch(lot=MOVIE, source=TMDB, query=<title>) → get Ryot metadataId
  2. deployBulkMetadataProgressUpdate([{metadataId, change: {createNewCompleted:
     {finishedOnDate: {timestamp}}}}])
  3. For unscrobble: query userMetadataDetails → history[].id → deleteSeenItem
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)


class RyotClient:
    def __init__(self, *, base_url: str, api_key: str) -> None:
        self._endpoint = base_url.rstrip("/") + "/backend/graphql"
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def startup(self) -> None:
        """Verify base URL + bearer with a tiny authenticated GraphQL query.
        `userDetails` requires auth; a missing/invalid token returns errors."""
        try:
            resp = await self._http.post(
                self._endpoint,
                json={"query": "query { userDetails { __typename } }"},
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Ryot unreachable at {self._endpoint}: {exc}") from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"Ryot returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"Ryot rejected credentials: {payload['errors']}")
        log.info("Ryot: credentials verified")

    async def _gql(self, query: str, variables: dict[str, Any]) -> Optional[dict[str, Any]]:
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
        data = await self._gql(query, variables)
        if not data:
            return None
        items = (
            (data.get("metadataSearch") or {}).get("response", {}).get("items") or []
        )
        if not items:
            return None
        # items are strings (metadataIds) in Ryot's current schema.
        return items[0] if isinstance(items[0], str) else items[0].get("identifier")

    async def scrobble(self, tmdb_id: int, watched_at: datetime, title: str) -> bool:
        metadata_id = await self._find_metadata_id(title, tmdb_id)
        if not metadata_id:
            log.warning("Ryot: could not resolve %s to a metadataId", title)
            return False

        timestamp = watched_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
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
                            "finishedOnDate": {"timestamp": timestamp}
                        }
                    },
                }
            ]
        }
        data = await self._gql(mutation, variables)
        if data is None:
            return False
        log.info("Ryot: scrobbled %s (metadataId=%s)", title, metadata_id)
        return True

    async def unscrobble(self, tmdb_id: int, watched_at: datetime | None, title: str) -> bool:
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
