# AGENTS.md

Notes for future contributors and AI coding agents editing this repo.
Keep this file **up to date** whenever the architecture, external API
contracts, or config surface changes.

## Purpose (do not lose sight of it)

ticketarr is a **headless** Python service. It has no UI and exposes no public
API. The only HTTP surface is `/healthz` on a private port for docker
healthchecks. Do not add public endpoints without an explicit user request.

## Design principles

1. **Lean, not clever.** A single asyncio process. No databases beyond a JSON
   state file. Prefer standard library + a few well-maintained deps.
2. **Config-first.** Every knob comes from `config.yml` OR environment
   variables. Env always wins. New settings must land in both
   `ticketarr/config.py` and `config.example.yml` / `.env.example`.
3. **Fail soft, log loud.** A single failing external service should never
   crash the loop. Log the error, keep processing the next email.
   _Exception:_ boot-time credential verification is fail-fast — see
   "Startup verification" below.
4. **Idempotent.** Every email carries a stable fingerprint (Message-ID or
   UIDVALIDITY/UID). Processing the same fingerprint twice must be a no-op.

## Startup verification

Every configured integration must prove its credentials at boot. This is done
by an `async startup()` method on the integration client:

- **IMAP** (`imap_monitor.IMAPMonitor.startup`) — connects, logs in,
  selects the mailbox read-only.
- **TMDB** (`tmdb.TMDBClient.startup`) — `GET /3/configuration`.
- **Trakt** (`integrations.trakt.TraktClient.startup`) — runs the device
  code flow if no token is cached; refreshes if expired.
- **Ryot** (`integrations.ryot.RyotClient.startup`) — runs the
  `loginUser` mutation (when username+password are configured) and then
  verifies with a `userDetails` GraphQL query.
- **Yamtrack** (`integrations.yamtrack.YamtrackClient.startup`) —
  logs into `/accounts/login/` and verifies the resulting session
  by fetching the home page.
- **Seerr** (`integrations.seerr.SeerrClient.startup`) — `GET /auth/me`.
- **Ombi** (`integrations.ombi.OmbiClient.startup`) —
  `GET /api/v1/Settings/about`.

The orchestrator (`Application._verify_all`) runs each `startup()` before
entering the IMAP polling loop. **Any failure is FATAL:** the process
logs the offending component and exits so docker/systemd restart policies
can retry. `/healthz` returns HTTP 503 with `{"status": "starting",
"components": {...}}` until every component reports "ok", then flips to
HTTP 200 with `{"status": "ok", ...}`.

When adding a new integration, implement `startup()`. Keep the call
cheap (single authenticated request) and always raise `RuntimeError` with
a clear, actionable message ("Ombi rejected the ApiKey (HTTP 401)") — the
message goes straight to the operator's logs and to `/healthz`.

## Layout

```
ticketarr/__main__.py        signal + asyncio bootstrap
ticketarr/app.py             orchestration: IMAP → parser → TMDB → tracker/requester
ticketarr/config.py          pydantic v2 config loader (YAML + env overlay)
ticketarr/imap_monitor.py    IMAPClient-based poller (multi-sender), runs blocking work in an executor
ticketarr/parsers/           per-chain email parsers
  __init__.py                registry + dispatcher (parse_email, default_sender_filters)
  base.py                    ParsedEmail dataclass + EmailParser Protocol
  util.py                    shared helpers (body_text, clean, date parsers)
  amc.py                     AMC A-List (ported from @ijoshi129/Marquee)
  regal.py                   Regal Unlimited (subject-line title + booking code)
ticketarr/state.py           JSON state store (`processed` set, `orders` map)
ticketarr/tmdb.py            TMDB /search/movie client
ticketarr/integrations/
  base.py                    Tracker + Requester Protocols
  trakt.py                   OAuth device flow + /sync/history
  ryot.py                    GraphQL: metadataSearch + deployBulkMetadataProgressUpdate
  yamtrack.py                Jellyfin-style webhook receiver
  seerr.py                   Jellyseerr/Overseerr /api/v1/request
  ombi.py                    Ombi /api/v1/Request/movie
```

## Data flow

```
IMAP poll → InboundEmail → parse_email(subject, html, text) → ParsedEmail
                                                                 │
                     reservation ────────────┐                   │ cancellation
                                             ▼                   ▼
                            TMDB.search_movie(title, year)   state.pop_order(order#)
                                             │                   │
                     tracker.scrobble(tmdb_id, showtime)     tracker.unscrobble(tmdb_id, …)
                     requester.request_movie(tmdb_id)             │
                                             │                   │
                     state.record_order(order#, tmdb_id, …)    (persist)
```

`state.json` is the source of truth for "which order maps to which TMDB id".
Do not remove the persistence step or cancellations for orders received after
a restart will lose their link to the original scrobble.

## External API references (verified during initial implementation)

- **TMDB v3** — https://developer.themoviedb.org/reference
  - `GET /3/search/movie?query=…&primary_release_year=…`
  - Auth: `?api_key=…` OR `Authorization: Bearer <v4 read token>`
- **Trakt v2** — https://docs.trakt.tv/
  - Device Code Flow: `POST /oauth/device/code` then poll `POST /oauth/device/token`
  - Scrobble: `POST /sync/history` with `{"movies":[{"watched_at":ISO_Z,"ids":{"tmdb":…}}]}`
  - Remove: `POST /sync/history/remove`
  - Headers on every call: `Content-Type: application/json`,
    `trakt-api-version: 2`, `trakt-api-key: <client_id>`,
    `Authorization: Bearer <access_token>`
- **Ryot** — https://docs.ryot.io + GraphQL schema at
  `libs/graphql/src/backend/{queries,mutations}/combined.gql`
  - `POST {base}/backend/graphql`
  - Auth is a **session token**, not a static "API key". The extractor
    (`crates/utils/application/src/lib.rs::AuthContext::from_request_parts`)
    accepts either `Authorization: Bearer <session>` or
    `x-auth-token: <session>` and resolves it via
    `session_service::validate_session`. Without a valid session,
    resolvers throw `NO_USER_ID`.
  - Login: `loginUser(input: {username, password})` mutation returns
    `ApiKeyResponse{apiKey}` where `apiKey` **is** the session token to
    use as the Bearer for all subsequent requests
    (`crates/services/user/src/authentication_operations.rs::login_user`).
    Handle the `LoginError` and `StringIdObject` (2FA) union branches
    with clear messages.
  - Verify at startup with `userDetails { ... on UserDetails { id } ... on UserDetailsError { error } }` — do **not** just ask for `__typename`;
    the error branch has to be inspected explicitly.
  - Search: `metadataSearch(input: {lot: MOVIE, source: TMDB, search:{query, page, take}})`.
    Ryot's resolver forwards the query to its **own** TMDB provider, which
    reads `MOVIES_AND_SHOWS_TMDB_ACCESS_TOKEN` (a TMDB v4 read token) from
    the Ryot container's environment — this is **separate from ticketarr's
    `tmdb.api_key`**. When Ryot's TMDB token is missing/invalid, the resolver
    swallows the concrete error via `trace_ok()`
    (`crates/services/miscellaneous/search/src/lib.rs`) and returns the
    generic `"Failed to search metadata"`. `RyotClient._find_metadata_id`
    logs an actionable hint pointing at this env var so operators aren't
    left guessing.
  - Scrobble: `deployBulkMetadataProgressUpdate([{metadataId,
change:{createNewCompleted:{startedAndFinishedOnDate:{startedOn,timestamp}}}}])`.
    `startedOn` is the showtime, `timestamp` is `startedOn + TMDB runtime`
    (falling back to `ryot.default_runtime_minutes`, 120 min by default) —
    same start/end logic Yamtrack uses so both trackers show the movie
    filling its real slot rather than a point event.
  - Remove: `deleteSeenItem(seenId)` after reading `userMetadataDetails.history[].id`
  - `ryot.api_key` in ticketarr's config is kept as an advanced fallback
    for users who prefer a pre-issued session token (e.g. from
    `processAccessLink`). Most users should set `ryot.username` +
    `ryot.password` and let ticketarr log in at startup.
- **Yamtrack** — **no official REST API** for creating watched-movie
  entries with a specific timestamp. The Jellyfin webhook path exists
  but overrides `watched_at` with `timezone.now()` server-side, so
  we cannot backdate through it. To honor real showtimes, ticketarr
  drives the same Django form endpoints that Yamtrack's own web UI
  posts to:
  - Login: `POST /accounts/login/` with `login`, `password`,
    `csrfmiddlewaretoken` (django-allauth's default login form).
    Session cookie + `csrftoken` cookie are reused thereafter with
    `X-CSRFToken` on every write.
  - Scrobble: `POST /media_save` with form fields
    `media_type=movie`, `source=tmdb`, `media_id=<tmdb>`,
    `status=Completed`, `score=`, `notes=`,
    `start_date=<YYYY-MM-DDTHH:MM>`, `end_date=<YYYY-MM-DDTHH:MM>`.
    An empty/absent `instance_id` creates a new Movie row (used
    unconditionally: rewatches are common with AMC A-List / Regal
    Unlimited, and always-creating never clobbers user-edited
    score/notes).
  - `end_date = start_date + runtime` (from TMDB `/movie/{id}.runtime`),
    falling back to 120 minutes when runtime is unknown.
  - Discover the new row's `instance_id`: `GET /track_modal/tmdb/movie/<tmdb>?return_url=/`,
    scrape `<input name="instance_id" value="…">` from the rendered
    form. The `Media` model's default ordering
    (`["user", "item", "-created_at"]`) guarantees this is the row
    we just created.
  - Unscrobble: `POST /media_delete` with `instance_id` (persisted in
    `state.json` under `orders[order#].tracker_ids["yamtrack"]`) and
    `media_type=movie`.
  - **These are unofficial internal endpoints.** A Yamtrack upgrade
    can change the field names, CSRF handling, or ordering without
    notice. Keep the whole Yamtrack contract confined to
    `integrations/yamtrack.py` so a future breakage is one-file to fix.
- **Jellyseerr / Overseerr** — https://api-docs.overseerr.dev/
  - `POST /api/v1/request` with `{"mediaType":"movie","mediaId":<tmdb>,"is4k":<bool>}`,
    `X-Api-Key: <key>`. The `is4k` field is optional (defaults to false);
    ticketarr sends it only when `seerr.request_4k` is true.
- **Ombi** — https://docs.ombi.app/
  - `POST /api/v1/Request/movie` with `{"theMovieDbId":<tmdb>}`, header
    `ApiKey: <key>` (note the header name is literally `ApiKey`, not
    `X-Api-Key`).
  - Ombi's V2 `RequestsController`
    (`src/Ombi/Controllers/V2/RequestsController.cs`) does **not** expose
    `POST movie` — only `POST movie/advancedoptions` and
    `POST movie/collection/{id}`. A `POST /api/v2/Requests/movie` lands
    on ASP.NET Core's SPA-fallback middleware and returns HTTP 500 with a
    body about "The SPA default page middleware could not return the
    default page '/index.html'". If that specific 500 body is seen, log
    an actionable error pointing at `ombi.base_url` misconfiguration
    (usually a missing reverse-proxy subpath).

## Parsers

Every theater chain is a self-contained parser module under
`ticketarr/parsers/`. A parser is any object that implements the
`EmailParser` protocol in `ticketarr/parsers/base.py`:

- `chain` — short id (`"amc"`, `"regal"`, …). Ends up in
  `ParsedEmail.source`.
- `sender_filters: list[str]` — canonical From-addresses. The IMAP monitor
  automatically searches the union of every registered parser's senders when
  `imap.sender_filters` isn't explicitly set.
- `can_parse(subject, from_addr, html, text) -> bool` — cheap sniff to
  claim an email. Prefer matching on `from_addr` first; fall back to
  subject/body markers.
- `parse(subject, html, text) -> ParsedEmail` — the real work. Must return
  a `ParsedEmail` (never raise); populate `kind`, `ok` /`error`, and
  the fields listed below.

Contract for reservations and cancellations: both must produce an
`order_number` (or another **stable per-reservation identifier** in that
same field) — the orchestrator uses it to correlate a cancellation with the
scrobble it needs to undo. Different chains can namespace their ids however
they like (e.g. `"regal:ABC123"`) as long as the reservation and its
cancellation land on the same value.

Parsers are registered in `ticketarr/parsers/__init__.py::REGISTRY`. The
regex anchors and skip heuristics in `parsers/amc.py` mirror the JS
implementation in
[`ijoshi129/Marquee`](https://github.com/ijoshi129/Marquee/tree/main/server/parsers).
When AMC changes the email format, update the classifier / block regexes
inside that module only. Add unit-style fixtures under `tests/` (not yet
present) before changing anchor logic.

### Adding a new theater chain (e.g. Regal Unlimited)

1. Collect at least one real sample of each email type (reservation,
   cancellation, and any post-visit "thank you" message).
2. Create `ticketarr/parsers/<chain>.py` and implement a class satisfying
   `EmailParser`. Reuse helpers from `parsers/util.py`
   (`body_text`, `clean`, `parse_time_then_date` etc.).
3. Add an instance to `REGISTRY` in `ticketarr/parsers/__init__.py`.
4. That's it. **No changes to** `app.py`, `imap_monitor.py`, `config.py`,
   `docker-compose.yml`, or the README's config surface are needed — the
   IMAP monitor will pick up the new sender automatically, and the
   orchestrator dispatches by `ParsedEmail.kind` regardless of chain.
5. Document the sender + supported email types in the README's "How it works"
   section and update the layout tree here.

## Adding a new provider

1. Add a config section in `ticketarr/config.py` (both the pydantic model and
   the env-overlay function).
2. Implement a client in `ticketarr/integrations/<name>.py` that satisfies the
   `Tracker` or `Requester` protocol in `integrations/base.py`. **Include an
   `async startup()`** that performs a cheap authenticated call and raises
   `RuntimeError` on failure — see "Startup verification" above.
3. Wire it into `_build_tracker` / `_build_requester` in `ticketarr/app.py`.
4. Extend `TrackerConfig` / `RequesterConfig` literal, update
   `config.example.yml`, `.env.example`, and the README provider table.

## What NOT to do

- Do not add a public HTTP API surface. If you need to expose state, add it
  behind the same private port as `/healthz`, not on a new listener.
- Do not switch to a real database. The JSON state file is intentional.
- Do not hard-code credentials or provider URLs.
- Do not silently swallow parser errors; always log the subject.

## Attribution

AMC email parsing logic is a port of the JS parsers by
[@ijoshi129](https://github.com/ijoshi129) in
[`ijoshi129/Marquee`](https://github.com/ijoshi129/Marquee). Keep the
attribution in `README.md` and in the module docstring of
`ticketarr/parsers/amc.py` when refactoring.
