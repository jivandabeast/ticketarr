"""Configuration loader.

At runtime we load config from either a YAML file (path given by
``TICKETARR_CONFIG`` or the first existing default) or the environment.

Environment variables always take precedence over YAML values for the same key.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

_DEFAULT_CONFIG_PATHS = (
    "/config/config.yml",
    "/config/config.yaml",
    "./config/config.yml",
    "./config/config.yaml",
    "./config.yml",
    "./config.yaml",
)


class IMAPConfig(BaseModel):
    host: str
    port: int = 993
    username: str
    password: str
    mailbox: str = "INBOX"
    ssl: bool = True
    # Skip messages older than this many days on first run (0 = process all unread).
    initial_lookback_days: int = 7
    # Sender addresses to match on. If left empty, the union of every registered
    # parser's ``sender_filters`` is used automatically. Set an explicit list
    # (e.g. ["AMCTheatres@amctheatres.com", "no-reply@regmovies.com"]) to
    # override, e.g. for testing or non-US locales.
    sender_filters: list[str] = Field(default_factory=list)
    # Poll interval in seconds when IDLE is not available.
    poll_interval_seconds: int = 120
    # Whether to mark processed messages as seen.
    mark_seen: bool = True


class TMDBConfig(BaseModel):
    # Either api_key (v3) OR bearer_token (v4 read access token) is required.
    api_key: Optional[str] = None
    bearer_token: Optional[str] = None

    @model_validator(mode="after")
    def _one_credential(self) -> "TMDBConfig":
        if not self.api_key and not self.bearer_token:
            raise ValueError("tmdb: provide either api_key or bearer_token")
        return self


class TraktConfig(BaseModel):
    enabled: bool = False
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    # Persisted OAuth token. If not present the app will run the device flow
    # once at startup and write it to ``token_path``.
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_expires_at: Optional[int] = None
    token_path: str = "/config/trakt_token.json"


class RyotConfig(BaseModel):
    enabled: bool = False
    base_url: Optional[str] = None  # e.g. http://ryot:8000 or https://app.ryot.io
    # Preferred: username + password. ticketarr will call `loginUser` at
    # startup to obtain a session token. This is what Ryot's own web UI does.
    username: Optional[str] = None
    password: Optional[str] = None
    # Advanced: a long-lived access-link token (`processAccessLink.apiKey`
    # from Ryot). Used verbatim as the Bearer token. If both are set,
    # username/password wins (it produces a fresh session on each restart).
    api_key: Optional[str] = None
    # Fallback duration (minutes) when TMDB doesn't report a runtime for the
    # movie. Mirrors Yamtrack so a scrobble always carries a plausible
    # finished-on timestamp instead of collapsing start == end.
    default_runtime_minutes: int = 120


class YamtrackConfig(BaseModel):
    enabled: bool = False
    base_url: Optional[str] = None  # e.g. http://yamtrack:8000
    # Yamtrack exposes no REST auth for creating watched-movie entries with a
    # specific timestamp; ticketarr drives the internal /media_save form,
    # which requires a real Django session. Provide the same
    # username/password you use for the Yamtrack web UI.
    username: Optional[str] = None
    password: Optional[str] = None
    # Legacy webhook token, no longer used (kept for state-file compatibility
    # with older configs). Yamtrack's Jellyfin webhook overrides the caller's
    # watched_at with server-side ``timezone.now()``, so ticketarr no longer
    # uses it.
    webhook_token: Optional[str] = None


class SeerrConfig(BaseModel):
    enabled: bool = False
    # "seerr" covers both Overseerr and Jellyseerr (same API).
    base_url: Optional[str] = None  # e.g. http://jellyseerr:5055
    api_key: Optional[str] = None
    request_4k: bool = False  # request 4K quality instead of standard


class OmbiConfig(BaseModel):
    enabled: bool = False
    base_url: Optional[str] = None  # e.g. http://ombi:3579
    api_key: Optional[str] = None
    language_code: str = "en"


class TrackerConfig(BaseModel):
    """Which tracker to report to. Only one is used at a time."""

    provider: Literal["none", "trakt", "ryot", "yamtrack"] = "none"


class RequesterConfig(BaseModel):
    """Which requester (if any) to also send a movie request to."""

    provider: Literal["none", "seerr", "ombi"] = "none"


class Config(BaseModel):
    imap: IMAPConfig
    tmdb: TMDBConfig
    tracker: TrackerConfig = TrackerConfig()
    requester: RequesterConfig = RequesterConfig()
    trakt: TraktConfig = TraktConfig()
    ryot: RyotConfig = RyotConfig()
    yamtrack: YamtrackConfig = YamtrackConfig()
    seerr: SeerrConfig = SeerrConfig()
    ombi: OmbiConfig = OmbiConfig()

    # State file used to dedupe processed emails across restarts.
    state_path: str = "/config/state.json"

    # How often (seconds) the pending-scrobble sweeper wakes to look for
    # reservations whose showtime has finally passed. Trakt rejects any
    # ``watched_at`` that is in the future, and marking a movie "watched"
    # before its showtime is semantically wrong on every tracker — so
    # reservations for future shows are persisted with ``scrobbled=False``
    # and dispatched by the sweeper. Setting this to 0 disables the sweeper
    # (the scrobble will still happen next time an email arrives or the
    # process restarts, thanks to the same on-boot sweep).
    pending_scrobble_check_interval_seconds: int = 300

    # Whether to expose a /healthz endpoint (recommended for docker healthchecks).
    healthcheck_port: int = 8765

    # Purely informational: which source loaded this config.
    source: str = "env"

    @model_validator(mode="after")
    def _tracker_wired(self) -> "Config":
        if self.tracker.provider == "trakt" and not self.trakt.enabled:
            self.trakt.enabled = True
        if self.tracker.provider == "ryot" and not self.ryot.enabled:
            self.ryot.enabled = True
        if self.tracker.provider == "yamtrack" and not self.yamtrack.enabled:
            self.yamtrack.enabled = True
        if self.requester.provider == "seerr" and not self.seerr.enabled:
            self.seerr.enabled = True
        if self.requester.provider == "ombi" and not self.ombi.enabled:
            self.ombi.enabled = True
        return self


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file {path} must contain a mapping at the top level")
    return data


def _env(name: str) -> Optional[str]:
    val = os.getenv(name)
    if val is None:
        return None
    val = val.strip()
    return val or None


def _env_bool(name: str) -> Optional[bool]:
    raw = _env(name)
    if raw is None:
        return None
    return raw.lower() in ("1", "true", "yes", "on")


def _env_int(name: str) -> Optional[int]:
    raw = _env(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Apply env vars on top of the dict, creating sub-dicts as needed."""

    def _set(section: str, key: str, value: Any) -> None:
        if value is None:
            return
        data.setdefault(section, {})[key] = value

    # IMAP
    _set("imap", "host", _env("IMAP_HOST"))
    _set("imap", "port", _env_int("IMAP_PORT"))
    _set("imap", "username", _env("IMAP_USERNAME"))
    _set("imap", "password", _env("IMAP_PASSWORD"))
    _set("imap", "mailbox", _env("IMAP_MAILBOX"))
    _set("imap", "ssl", _env_bool("IMAP_SSL"))
    _set("imap", "initial_lookback_days", _env_int("IMAP_INITIAL_LOOKBACK_DAYS"))
    senders_raw = _env("IMAP_SENDER_FILTERS") or _env("IMAP_SENDER_FILTER")
    if senders_raw is not None:
        senders = [s.strip() for s in senders_raw.split(",") if s.strip()]
        _set("imap", "sender_filters", senders)
    _set("imap", "poll_interval_seconds", _env_int("IMAP_POLL_INTERVAL_SECONDS"))
    _set("imap", "mark_seen", _env_bool("IMAP_MARK_SEEN"))

    # TMDB
    _set("tmdb", "api_key", _env("TMDB_API_KEY"))
    _set("tmdb", "bearer_token", _env("TMDB_BEARER_TOKEN"))

    # Tracker/requester selection
    _set("tracker", "provider", _env("TRACKER_PROVIDER"))
    _set("requester", "provider", _env("REQUESTER_PROVIDER"))

    # Trakt
    _set("trakt", "enabled", _env_bool("TRAKT_ENABLED"))
    _set("trakt", "client_id", _env("TRAKT_CLIENT_ID"))
    _set("trakt", "client_secret", _env("TRAKT_CLIENT_SECRET"))
    _set("trakt", "access_token", _env("TRAKT_ACCESS_TOKEN"))
    _set("trakt", "refresh_token", _env("TRAKT_REFRESH_TOKEN"))
    _set("trakt", "token_path", _env("TRAKT_TOKEN_PATH"))

    # Ryot
    _set("ryot", "enabled", _env_bool("RYOT_ENABLED"))
    _set("ryot", "base_url", _env("RYOT_BASE_URL"))
    _set("ryot", "username", _env("RYOT_USERNAME"))
    _set("ryot", "password", _env("RYOT_PASSWORD"))
    _set("ryot", "api_key", _env("RYOT_API_KEY"))
    _set("ryot", "default_runtime_minutes", _env_int("RYOT_DEFAULT_RUNTIME_MINUTES"))

    # Yamtrack
    _set("yamtrack", "enabled", _env_bool("YAMTRACK_ENABLED"))
    _set("yamtrack", "base_url", _env("YAMTRACK_BASE_URL"))
    _set("yamtrack", "username", _env("YAMTRACK_USERNAME"))
    _set("yamtrack", "password", _env("YAMTRACK_PASSWORD"))
    _set("yamtrack", "webhook_token", _env("YAMTRACK_WEBHOOK_TOKEN"))

    # Seerr (Jellyseerr / Overseerr)
    _set("seerr", "enabled", _env_bool("SEERR_ENABLED"))
    _set("seerr", "base_url", _env("SEERR_BASE_URL"))
    _set("seerr", "api_key", _env("SEERR_API_KEY"))
    _set("seerr", "request_4k", _env_bool("SEERR_REQUEST_4K"))

    # Ombi
    _set("ombi", "enabled", _env_bool("OMBI_ENABLED"))
    _set("ombi", "base_url", _env("OMBI_BASE_URL"))
    _set("ombi", "api_key", _env("OMBI_API_KEY"))
    _set("ombi", "language_code", _env("OMBI_LANGUAGE_CODE"))

    # Misc
    _set_root = data
    if (v := _env("TICKETARR_STATE_PATH")) is not None:
        _set_root["state_path"] = v
    if (v := _env_int("TICKETARR_HEALTHCHECK_PORT")) is not None:
        _set_root["healthcheck_port"] = v
    if (v := _env_int("TICKETARR_PENDING_SCROBBLE_CHECK_INTERVAL_SECONDS")) is not None:
        _set_root["pending_scrobble_check_interval_seconds"] = v

    return data


def load_config() -> Config:
    """Load config from YAML (if present) then overlay environment variables."""
    data: dict[str, Any] = {}
    source = "env"

    yaml_path = _env("TICKETARR_CONFIG")
    if yaml_path:
        p = Path(yaml_path)
        if not p.exists():
            raise FileNotFoundError(f"TICKETARR_CONFIG={yaml_path} does not exist")
        data = _load_yaml(p)
        source = str(p)
    else:
        for candidate in _DEFAULT_CONFIG_PATHS:
            p = Path(candidate)
            if p.exists():
                data = _load_yaml(p)
                source = str(p)
                break

    data = _apply_env_overrides(data)
    data["source"] = source

    return Config.model_validate(data)
