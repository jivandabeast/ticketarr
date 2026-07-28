# ticketarr

A tiny, headless Python service that watches an email inbox for AMC A-List
reservation / cancellation emails and mirrors them to your movie tracker
(Trakt, Ryot, or Yamtrack). Optionally submits a request for the same movie to
Jellyseerr/Overseerr ("Seer") or Ombi.

Support for additional theater chains (e.g. Regal Unlimited) is designed to
drop in as a single new parser module — see
[AGENTS.md](./AGENTS.md#adding-a-new-theater-chain-eg-regal-unlimited).

No UI. No public endpoints. Just a docker-native background worker.

## How it works

1. Log into your IMAP inbox and look for messages from
   `AMCTheatres@amctheatres.com`.
2. Classify each message (reservation / cancellation / other).
3. Parse the movie title, theater, showtime, and order number.
4. Resolve the movie to a TMDB id.
5. Report the movie to the configured tracker with the showtime as the
   watched-at timestamp. On a cancellation, remove the corresponding history
   entry.
6. If a requester is configured, also submit a movie request.

Order numbers are persisted (`/config/state.json`) so cancellations can undo
their matching reservation.

## Quick start

```
git clone <this-repo> ticketarr && cd ticketarr

# Option 1: YAML config
cp config.example.yml config/config.yml
$EDITOR config/config.yml

# Option 2: environment variables
cp .env.example .env
$EDITOR .env
# then uncomment `env_file: - .env` in docker-compose.yml

docker compose up -d --build
docker compose logs -f ticketarr
```

The healthcheck endpoint listens on `http://127.0.0.1:8765/healthz`.

## Configuration

ticketarr loads config in this order, with later sources overriding earlier
ones:

1. `TICKETARR_CONFIG` env var → path to a YAML file
2. First existing default: `/config/config.yml`, `/config/config.yaml`,
   `./config.yml`, `./config.yaml`
3. Environment variables (override anything above)

See [`config.example.yml`](./config.example.yml) and
[`.env.example`](./.env.example) for the full list of options.

### Required

- IMAP host + credentials for the inbox that receives AMC emails
- **TMDB** API key (v3) or bearer token (v4 read access token)
- One of the tracker providers (`trakt`, `ryot`, `yamtrack`) — or `none` if
  you only want to submit requests

### Trakt bootstrap

On first run with `tracker.provider: trakt`, the app performs Trakt's OAuth
Device Flow. It logs a message like:

```
Trakt authorization required: go to https://trakt.tv/activate and enter code XXXXXXXX
```

Visit that URL in a browser, sign in, and enter the code. The resulting
tokens are written to `trakt.token_path` (default `/config/trakt_token.json`)
and reused on subsequent runs.

### Provider notes

| Provider               | Auth                                          | Notes                                                                                                                        |
| ---------------------- | --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Trakt.tv               | OAuth (device flow), needs client id + secret | Full watched/unwatched support with showtime timestamp                                                                       |
| Ryot                   | Long-lived API token                          | GraphQL against `/backend/graphql`; scrobbles via `deployBulkMetadataProgressUpdate`                                         |
| Yamtrack               | Per-user webhook token                        | Uses Jellyfin-style `POST /webhook/jellyfin/<token>`. Watched-at is set server-side (Yamtrack limitation) — cannot backdate. |
| Jellyseerr / Overseerr | `X-Api-Key` header                            | `POST /api/v1/request` with `{"mediaType":"movie","mediaId":<tmdb>}`                                                         |
| Ombi                   | `ApiKey` header                               | Prefers `POST /api/v2/Requests/movie`; falls back to `/api/v1/Request/movie`                                                 |

## Repository layout

```
ticketarr/                    Python package
  ├─ __main__.py              entrypoint
  ├─ app.py                   orchestrator
  ├─ config.py                YAML + env config loader (pydantic)
  ├─ imap_monitor.py          IMAP polling / message decode (multi-sender)
  ├─ parsers/                 per-chain email parsers (registry-based)
  │    ├─ __init__.py         dispatcher + REGISTRY
  │    ├─ base.py             ParsedEmail + EmailParser protocol
  │    ├─ util.py             shared regex/date helpers
  │    └─ amc.py              AMC A-List (ported from Marquee)
  ├─ state.py                 JSON state store (dedupe + order → tmdb map)
  ├─ tmdb.py                  TMDB search client
  └─ integrations/
       ├─ base.py             Tracker / Requester protocols
       ├─ trakt.py
       ├─ ryot.py
       ├─ yamtrack.py
       ├─ seer.py
       └─ ombi.py
Dockerfile
docker-compose.yml
config.example.yml
.env.example
```

## Attribution

The AMC A-List email parsers in [`ticketarr/parsers/amc.py`](./ticketarr/parsers/amc.py)
were ported from the JavaScript parsers in
[`ijoshi129/Marquee`](https://github.com/ijoshi129/Marquee/tree/main/server/parsers)
by [@ijoshi129](https://github.com/ijoshi129). Thank you for the work.

## Development

```
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m ticketarr
```

### Running the tests

The parsers are the highest-risk surface, so the test suite is fixture-driven:
drop **sanitized real emails** into `tests/fixtures/<chain>/` as `.eml` files
and pytest will parametrize the parser assertions over every one.

```
pytest
```

Filename conventions and optional `<name>.expected.json` sidecar assertions
are documented in [`tests/fixtures/amc/README.md`](./tests/fixtures/amc/README.md).
The suite runs green even when the fixtures directory is empty, so you can
commit the harness now and add real emails later.

See [`AGENTS.md`](./AGENTS.md) for architecture notes and pointers relevant
for future edits.

## License

See [`LICENSE`](./LICENSE).
