"""Entry point for `python -m ticketarr` and the `ticketarr` console script."""

from __future__ import annotations

import asyncio
import logging
import signal

from .app import Application
from .config import load_config
from .logging_setup import configure_logging


def main() -> None:
    configure_logging()
    log = logging.getLogger("ticketarr")

    cfg = load_config()
    log.info("Loaded configuration (source=%s)", cfg.source)

    app = Application(cfg)

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
