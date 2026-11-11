from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict

from .api import Fill, Trader

ALERT_TTL_S = 3 * 86400


class State:
    """What the bot remembers between runs, so nothing is alerted twice."""

    def __init__(self, data: dict | None = None):
        d = data or {}
        self.started: str | None = d.get("started")
        self.wallets: dict[str, dict] = dict(d.get("wallets", {}))   # wallet -> name/rank/pnl
        self.wallets_refreshed: int = int(d.get("wallets_refreshed", 0))
        self.cursors: dict[str, int] = dict(d.get("cursors", {}))    # wallet -> last fetch ts
        self.fills: dict[str, dict] = dict(d.get("fills", {}))       # uid -> fill
        self.alerted: dict[str, int] = dict(d.get("alerted", {}))    # alert key -> ts

    @property
    def is_new(self) -> bool:
        return not self.started

    @classmethod
    def load(cls, path: str) -> "State":
        if not os.path.exists(path):
            return cls()
        try:
            with open(path) as f:
                return cls(json.load(f))
        except json.JSONDecodeError as e:
            raise SystemExit(f"State file {path} is corrupt ({e}). "
                             "Delete it deliberately to start fresh.")

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump({"started": self.started, "wallets": self.wallets,
                       "wallets_refreshed": self.wallets_refreshed, "cursors": self.cursors,
                       "fills": self.fills, "alerted": self.alerted},
                      f, indent=1, sort_keys=True)
        os.replace(tmp, path)

    def set_wallets(self, traders: list[Trader], now_ts: int) -> None:
        self.wallets = {t.wallet: {"name": t.name, "rank": t.rank, "pnl": t.pnl}
                        for t in traders}
        self.wallets_refreshed = now_ts
        self.cursors = {w: c for w, c in self.cursors.items() if w in self.wallets}

    def add_fills(self, fills: list[Fill]) -> int:
        new = 0
        for f in fills:
            if f.uid not in self.fills:
                new += 1
                self.fills[f.uid] = asdict(f)
        return new

    def all_fills(self) -> list[Fill]:
        return [Fill(**d) for d in self.fills.values()]

    def mark_alerted(self, key: str, now_ts: int) -> None:
        self.alerted[key] = now_ts

    def prune(self, now_ts: int, window_hours: int) -> None:
        cutoff = now_ts - (window_hours + 6) * 3600
        self.fills = {k: v for k, v in self.fills.items() if v["ts"] >= cutoff}
        self.alerted = {k: v for k, v in self.alerted.items() if v >= now_ts - ALERT_TTL_S}
