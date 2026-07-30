"""Entry point for `python -m ticketarr` and the `ticketarr` console script."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from pydantic import ValidationError

from .app import Application
from .config import load_config
from .logging_setup import configure_logging


def _format_config_error(exc: ValidationError) -> str:
    """Turn a pydantic ValidationError into an operator-friendly list of
    missing/invalid fields, keyed by the env var the user should set."""
    # Map (section, field) -> env var name so the message points the
    # operator at the exact knob to set. Kept in sync with
    # ``config._apply_env_overrides``.
    env_hint = {
        ("imap", "host"): "IMAP_HOST",
        ("imap", "username"): "IMAP_USERNAME",
        ("imap", "password"): "IMAP_PASSWORD",
        ("tmdb", "api_key"): "TMDB_API_KEY",
        ("tmdb", "bearer_token"): "TMDB_BEARER_TOKEN",
    }
    lines: list[str] = []
    for err in exc.errors():
        loc = err.get("loc", ())
        msg = err.get("msg", "invalid value")
        dotted = ".".join(str(p) for p in loc) or "<root>"
        hint = ""
        if len(loc) >= 2:
            hint_key = (str(loc[0]), str(loc[-1]))
            if hint_key in env_hint:
                hint = f" (set env var {env_hint[hint_key]} or add to config.yml)"
        lines.append(f"  - {dotted}: {msg}{hint}")
    return "\n".join(lines)


def main() -> None:
    configure_logging()
    log = logging.getLogger("ticketarr")

    try:
        cfg = load_config()
    except FileNotFoundError as exc:
        log.error("Configuration error: %s", exc)
        log.error(
            "Point TICKETARR_CONFIG at a valid YAML file, place one at "
            "/config/config.yml, or set the required env vars (see .env.example)."
        )
        sys.exit(2)
    except ValidationError as exc:
        log.error(
            "Configuration is incomplete or invalid. "
            "ticketarr cannot start until the following are provided "
            "(via config.yml or environment variables):\n%s",
            _format_config_error(exc),
        )
        log.error(
            "See config.example.yml / .env.example for the full list of "
            "supported keys."
        )
        sys.exit(2)
    except ValueError as exc:
        # e.g. TMDB one-credential rule, malformed YAML top-level.
        log.error("Configuration error: %s", exc)
        sys.exit(2)

    log.info("Loaded configuration (source=%s)", cfg.source)

    try:
        app = Application(cfg)
    except RuntimeError as exc:
        # Raised by _build_tracker / _build_requester when the selected
        # provider is missing its required credentials, and by the
        # sender_filters check.
        log.error("Startup aborted: %s", exc)
        log.error(
            "Fix the configuration (see config.example.yml / .env.example) and restart."
        )
        sys.exit(2)

    loop = asyncio.new_event_loop()

    def _stop(*_: object) -> None:
        log.info("Shutdown signal received")
        loop.create_task(app.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:  # pragma: no cover - windows
            signal.signal(sig, lambda *_: _stop())

    try:
        loop.run_until_complete(app.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
