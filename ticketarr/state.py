"""Persistent state.

Two things live here:

- ``processed`` — a set of Message-Id / (uid, uidvalidity) fingerprints so we
  never process the same email twice across restarts.
- ``orders`` — maps AMC ``order_number`` → the last known info (tmdb id, title,
  scrobble timestamp). Used to undo (remove-from-history) a cancellation.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class OrderRecord:
    order_number: str
    tmdb_id: Optional[int]
    title: Optional[str]
    watched_at: Optional[str]  # ISO 8601 UTC
    theater_name: Optional[str] = None


@dataclass
class State:
    processed: set[str] = field(default_factory=set)
    orders: dict[str, OrderRecord] = field(default_factory=dict)


class StateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self.state = State()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self.state.processed = set(raw.get("processed", []))
        orders = raw.get("orders", {})
        self.state.orders = {
            k: OrderRecord(**v) for k, v in orders.items() if isinstance(v, dict)
        }

    async def save(self) -> None:
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "processed": sorted(self.state.processed),
                "orders": {k: asdict(v) for k, v in self.state.orders.items()},
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def already_processed(self, fingerprint: str) -> bool:
        return fingerprint in self.state.processed

    def mark_processed(self, fingerprint: str) -> None:
        self.state.processed.add(fingerprint)

    def record_order(self, record: OrderRecord) -> None:
        self.state.orders[record.order_number] = record

    def pop_order(self, order_number: str) -> Optional[OrderRecord]:
        return self.state.orders.pop(order_number, None)

    def get_order(self, order_number: str) -> Optional[OrderRecord]:
        return self.state.orders.get(order_number)
