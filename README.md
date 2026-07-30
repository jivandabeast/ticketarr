# ticketarr

<p align="center">
  <img src="./icon.svg" alt="ticketarr" width="160" height="160">
</p>

A tiny, headless Python service that watches an email inbox for AMC A-List
and Regal Unlimited reservation / cancellation emails and mirrors them to
your movie tracker (Trakt, Ryot, or Yamtrack). Optionally submits a request
for the same movie to Jellyseerr/Overseerr ("Seerr") or Ombi.

Support for additional theater chains is designed to drop in as a single
new parser module — see
[AGENTS.md](./AGENTS.md#adding-a-new-theater-chain-eg-regal-unlimited).

No UI. No public endpoints. Just a docker-native background worker.

## How it works

1. Log into your IMAP inbox and look for messages from any registered
   parser's canonical sender addresses. Out of the box:
   - **AMC A-List** — `AMCTheatres@amctheatres.com`
   - **Regal Unlimited** — `tickets@regaltickets.com` (reservations),
     `noreply@regaltickets.com` (refunds)
2. Classify each message (reservation / cancellation / other).
3. Parse the movie title, theater, showtime, and order/booking number.
   Regal reservation emails encode the real theater + showtime inside an
   inline JPEG, so ticketarr OCRs the ticket with Tesseract (bundled in
   the Docker image) and falls back to email-received time only when
   OCR fails.
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

docker compose up -d
docker compose logs -f ticketarr
```

The healthcheck endpoint listens on `http://127.0.0.1:8765/healthz`.

## Unraid (Community Apps)

ticketarr ships with a Community Apps template
([`templates/ticketarr.xml`](./templates/ticketarr.xml)) and a matching
[`ca_profile.xml`](./ca_profile.xml) so it can be installed directly from
the Apps tab.

### Two ways to configure — pick one

1. **Environment variables.** Fill in the IMAP + TMDB + tracker/requester
   fields on the template and click Apply. Every knob has a matching env
   var; see the template descriptions.
2. **YAML file.** Leave every field on the template blank, click Apply,
   then look in `/mnt/user/appdata/ticketarr/` for the `config.yml.sample`
   that ticketarr drops on first run. Copy it to `config.yml`, edit it,
   and restart the container.

If both are used, environment variables win. Empty env-var fields are
treated as "not set" (they do **not** clobber a real value in
`config.yml`), so you can safely leave anything you don't care about
blank on the template.

None of the template fields are marked strictly required because either
path is valid — but the container will refuse to start and print a clear
error listing the missing keys if IMAP host/username/password or a TMDB
credential is missing from **both** sources.

### First-run checklist

1. Install ticketarr from Community Apps (or point the CA "Install
   template from URL" dialog at
   `https://raw.githubusercontent.com/jivandabeast/ticketarr/main/templates/ticketarr.xml`).
2. Set the **Appdata** path (default `/mnt/user/appdata/ticketarr`).
3. Either fill in the env-var fields _or_ start the container, then edit
   `/mnt/user/appdata/ticketarr/config.yml` (copied from `.sample`).
4. Check the container log — you should see `Loaded configuration
(source=…)` followed by each integration's `startup ok` line.
5. If you selected Trakt as the tracker, the log will print a
   `https://trakt.tv/activate` URL and a one-time code — visit it in a
   browser to complete the OAuth device flow. The token is cached in
   `/config/trakt_token.json` and reused on every restart.

The healthcheck endpoint is on the container's private network at
`http://<container>:8765/healthz`. There is no WebUI — clicking the
container's icon in Unraid will not open anything useful.

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

- IMAP host + credentials for the inbox that receives ticket emails
- **TMDB** API key (v3) or bearer token (v4 read access token)
- One of the tracker providers (`trakt`, `ryot`, `yamtrack`) — or `none` if
  you only want to submit requests

### Filtering senders

By default ticketarr polls for messages from every registered parser's
canonical From-addresses (`AMCTheatres@amctheatres.com`,
`tickets@regaltickets.com`, `noreply@regaltickets.com`). If you want to
poll a different / additional set of senders — for example, a friend who
forwards you tickets, or a SimpleLogin / 33mail alias — set an explicit
list.

YAML (`imap.sender_filters` in `config.yml`):

```yaml
imap:
  sender_filters:
    - AMCTheatres@amctheatres.com
    - tickets@regaltickets.com
    - noreply@regaltickets.com
    - friend@gmail.com
```

Environment variable (`IMAP_SENDER_FILTERS`, comma-separated):

```bash
IMAP_SENDER_FILTERS="AMCTheatres@amctheatres.com,tickets@regaltickets.com,noreply@regaltickets.com,friend@gmail.com"
```

Setting this explicitly **replaces** the default list — include every
sender you want, not just the extras. Whitespace around commas is
trimmed. An empty value means "fall back to the registered defaults".

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

| Provider               | Auth                                          | Notes                                                                                                                                                                                                                                                                               |
| ---------------------- | --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Trakt.tv               | OAuth (device flow), needs client id + secret | Full watched/unwatched support with showtime timestamp                                                                                                                                                                                                                              |
| Ryot                   | Long-lived API token                          | GraphQL against `/backend/graphql`; scrobbles via `deployBulkMetadataProgressUpdate`                                                                                                                                                                                                |
| Yamtrack               | Web-UI username + password                    | Drives the internal `/media_save` Django form (Yamtrack has no REST API for setting a specific watched-at). `end_date` = `start_date + TMDB runtime` (fallback 120 min). Always-creates a new row per showing so rewatches are preserved. Contract is unofficial — see `AGENTS.md`. |
| Jellyseerr / Overseerr | `X-Api-Key` header                            | `POST /api/v1/request` with `{"mediaType":"movie","mediaId":<tmdb>, "is4k": <bool>}` (opt-in via `seerr.request_4k`)                                                                                                                                                                |
| Ombi                   | `ApiKey` header                               | Prefers `POST /api/v2/Requests/movie`; falls back to `/api/v1/Request/movie`                                                                                                                                                                                                        |

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
  │    ├─ amc.py              AMC A-List (ported from Marquee)
  │    └─ regal.py            Regal Unlimited
  ├─ state.py                 JSON state store (dedupe + order → tmdb map)
  ├─ tmdb.py                  TMDB search client
  └─ integrations/
       ├─ base.py             Tracker / Requester protocols
       ├─ trakt.py
       ├─ ryot.py
       ├─ yamtrack.py
       ├─ seerr.py
       └─ ombi.py
Dockerfile
docker-compose.yml
config.example.yml
.env.example
templates/ticketarr.xml         Unraid Community Apps template
ca_profile.xml                  Unraid CA repository profile
icon.svg                        used by CA and the README
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
