"""End-to-end tests for the Yamtrack client using ``httpx.MockTransport``.

We don't spin up a real Yamtrack instance; instead we simulate the exact
sequence of HTTP calls the client is supposed to make (login → media_save
→ track_modal → media_delete) and assert on the form fields we sent and
the returned instance_id.

The point of these tests is to catch regressions in:

- the login form field names (``login`` / ``password`` / ``csrfmiddlewaretoken``);
- the /media_save form contract (all six required fields present, empty
  ``instance_id`` for always-create, correct ``datetime-local`` formatting
  of ``start_date`` / ``end_date``);
- the end_date fallback when TMDB runtime is missing (120 min);
- CSRF cookie plumbing (X-CSRFToken sent on every write);
- the instance_id scrape from the ``track_modal`` HTML response.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from ticketarr.integrations.yamtrack import YamtrackClient


# The bits of Yamtrack HTML we care about. Real Yamtrack pages are much
# bigger but the client only needs these fields to work.
_LOGIN_HTML = (
    "<html><body><form>"
    '<input name="csrfmiddlewaretoken" value="csrf-from-login-page">'
    "</form></body></html>"
)
_HOME_LOGGED_IN = (
    "<html><body><nav>"
    '<form action="/accounts/logout/"><button>Logout</button></form>'
    "</nav></body></html>"
)
_TRACK_MODAL_HTML = (
    "<html><body><form>"
    '<input type="hidden" name="media_id" value="614696">'
    '<input type="hidden" name="source" value="tmdb">'
    '<input type="hidden" name="media_type" value="movie">'
    '<input type="hidden" name="instance_id" value="21455">'
    "</form></body></html>"
)


class _FakeYamtrack:
    """A single-call MockTransport handler that records every request."""

    def __init__(self, *, media_save_status: int = 302, login_status: int = 302) -> None:
        self.requests: list[httpx.Request] = []
        self.media_save_status = media_save_status
        self.login_status = login_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        method = request.method

        if method == "GET" and path == "/accounts/login/":
            # Prime the CSRF cookie so the client also has a cookie-based
            # fallback, matching real Django behaviour.
            return httpx.Response(
                200,
                text=_LOGIN_HTML,
                headers={"set-cookie": "csrftoken=csrf-cookie-value; Path=/"},
            )

        if method == "POST" and path == "/accounts/login/":
            if self.login_status == 302:
                return httpx.Response(302, headers={"location": "/"})
            return httpx.Response(
                200,
                text="<html><body>Invalid credentials</body></html>",
            )

        if method == "GET" and path == "/":
            return httpx.Response(200, text=_HOME_LOGGED_IN)

        if method == "POST" and path == "/media_save":
            return httpx.Response(self.media_save_status, headers={"location": "/"})

        if method == "GET" and path.startswith("/track_modal/"):
            return httpx.Response(200, text=_TRACK_MODAL_HTML)

        if method == "POST" and path == "/media_delete":
            return httpx.Response(302, headers={"location": "/"})

        return httpx.Response(404, text=f"unexpected {method} {path}")


def _client_with(handler: _FakeYamtrack, **kwargs: Any) -> YamtrackClient:
    """Build a YamtrackClient wired to the mock transport."""
    client = YamtrackClient(
        base_url="http://yamtrack.test",
        username="alice",
        password="hunter2",
        **kwargs,
    )
    # Replace the internal httpx client with one bound to our transport.
    # Keep the same cookie jar so the login side-effects on cookies still work.
    client._http = httpx.AsyncClient(
        base_url="http://yamtrack.test",
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    return client


def _form(body: bytes) -> dict[str, str]:
    """Parse an application/x-www-form-urlencoded body."""
    from urllib.parse import parse_qsl

    return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))


@pytest.mark.asyncio
async def test_startup_login_flow_sets_session():
    handler = _FakeYamtrack()
    client = _client_with(handler)
    try:
        await client.startup()
    finally:
        await client.aclose()

    paths = [(r.method, r.url.path) for r in handler.requests]
    assert paths == [
        ("GET", "/accounts/login/"),
        ("POST", "/accounts/login/"),
        ("GET", "/"),
    ]
    login_post = handler.requests[1]
    body = _form(login_post.content)
    assert body["login"] == "alice"
    assert body["password"] == "hunter2"
    # Either the HTML-scraped token or the cookie-derived one is acceptable.
    assert body["csrfmiddlewaretoken"] in {"csrf-from-login-page", "csrf-cookie-value"}


@pytest.mark.asyncio
async def test_startup_rejects_bad_credentials():
    handler = _FakeYamtrack(login_status=200)
    client = _client_with(handler)
    try:
        with pytest.raises(RuntimeError, match="rejected"):
            await client.startup()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_scrobble_sends_expected_form_and_returns_instance_id():
    handler = _FakeYamtrack()
    client = _client_with(handler)
    try:
        await client.startup()
        watched_at = datetime(2026, 7, 28, 19, 30, tzinfo=timezone.utc)
        instance_id = await client.scrobble(
            tmdb_id=614696,
            watched_at=watched_at,
            title="Some Movie",
            runtime_minutes=97,
        )
    finally:
        await client.aclose()

    assert instance_id == 21455

    save = next(
        r for r in handler.requests if r.method == "POST" and r.url.path == "/media_save"
    )
    body = _form(save.content)
    # Contract check: every field Yamtrack's MovieForm expects must be present.
    assert body["media_type"] == "movie"
    assert body["source"] == "tmdb"
    assert body["media_id"] == "614696"
    assert body["status"] == "Completed"
    assert body["score"] == ""
    assert body["notes"] == ""
    # Always-create: empty (or absent) instance_id.
    assert body.get("instance_id", "") == ""
    # datetime-local format: "YYYY-MM-DDTHH:MM" (no seconds, no tz)
    assert body["start_date"] == "2026-07-28T19:30"
    assert body["end_date"] == "2026-07-28T21:07"  # 19:30 + 97 min
    # CSRF header is set on every write.
    assert save.headers.get("x-csrftoken")


@pytest.mark.asyncio
async def test_scrobble_falls_back_to_120min_when_runtime_missing():
    handler = _FakeYamtrack()
    client = _client_with(handler)
    try:
        await client.startup()
        watched_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        await client.scrobble(tmdb_id=42, watched_at=watched_at, title="No Runtime")
    finally:
        await client.aclose()

    save = next(
        r for r in handler.requests if r.method == "POST" and r.url.path == "/media_save"
    )
    body = _form(save.content)
    assert body["start_date"] == "2026-01-01T12:00"
    assert body["end_date"] == "2026-01-01T14:00"  # +120 min


@pytest.mark.asyncio
async def test_unscrobble_uses_provided_instance_id():
    handler = _FakeYamtrack()
    client = _client_with(handler)
    try:
        await client.startup()
        ok = await client.unscrobble(
            tmdb_id=614696,
            watched_at=None,
            title="Some Movie",
            instance_id=21455,
        )
    finally:
        await client.aclose()

    assert ok is True
    delete = next(
        r for r in handler.requests if r.method == "POST" and r.url.path == "/media_delete"
    )
    body = _form(delete.content)
    assert body["instance_id"] == "21455"
    assert body["media_type"] == "movie"
    # Cleanup path must NOT need a track_modal round-trip when we already
    # know the instance_id.
    modal_hits = [r for r in handler.requests if r.url.path.startswith("/track_modal/")]
    assert modal_hits == []


@pytest.mark.asyncio
async def test_unscrobble_looks_up_instance_id_when_missing():
    handler = _FakeYamtrack()
    client = _client_with(handler)
    try:
        await client.startup()
        ok = await client.unscrobble(
            tmdb_id=614696,
            watched_at=None,
            title="Some Movie",
        )
    finally:
        await client.aclose()

    assert ok is True
    # Fallback path DID hit track_modal to discover the id.
    modal_hits = [r for r in handler.requests if r.url.path.startswith("/track_modal/")]
    assert len(modal_hits) == 1
    delete = next(
        r for r in handler.requests if r.method == "POST" and r.url.path == "/media_delete"
    )
    body = _form(delete.content)
    assert body["instance_id"] == "21455"


@pytest.mark.asyncio
async def test_scrobble_returns_none_when_media_save_fails():
    handler = _FakeYamtrack(media_save_status=500)
    client = _client_with(handler)
    try:
        await client.startup()
        instance_id = await client.scrobble(
            tmdb_id=1, watched_at=datetime(2026, 1, 1, tzinfo=timezone.utc), title="oops"
        )
    finally:
        await client.aclose()

    assert instance_id is None
