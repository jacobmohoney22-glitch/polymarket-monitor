from __future__ import annotations

import json
import os
import tempfile

from .api import Fill, Trader

BUCKET_S = 900              # stakes are summed into 15-minute buckets (keeps state small)
SEEN_TTL_S = 6 * 3600       # remember fill ids this long, to de-duplicate re-reads
ALERT_TTL_S = 3 * 86400


class State:
    """What the bot remembers between runs, so nothing is alerted twice.

    Individual fills are NOT stored: bots can make hundreds per day, so each wallet's buys on a
    pick are summed into 15-minute buckets. Only fill ids from the last few hours are kept, to
    avoid double-counting when a run re-reads its overlap window.
    """

    def __init__(self, data: dict | None = None):
        d = data or {}
        self.started: str | None = d.get("started")
        self.wallets: dict[str, dict] = dict(d.get("wallets", {}))    # wallet -> name/rank/pnl
        self.wallets_refreshed: int = int(d.get("wallets_refreshed", 0))
        self.cursors: dict[str, int] = dict(d.get("cursors", {}))     # wallet -> last fetch ts
        self.markets: dict[str, dict] = dict(d.get("markets", {}))    # cond -> title/event/outcomes
        self.stakes: dict[str, dict] = dict(d.get("stakes", {}))      # "wallet|cond|oi" -> buckets
        self.seen: dict[str, int] = dict(d.get("seen", {}))           # fill uid -> ts
        self.alerted: dict[str, int] = dict(d.get("alerted", {}))     # alert key -> ts

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
                       "markets": self.markets, "stakes": self.stakes, "seen": self.seen,
                       "alerted": self.alerted}, f, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, path)

    def set_wallets(self, traders: list[Trader], now_ts: int) -> None:
        self.wallets = {t.wallet: {"name": t.name, "rank": t.rank, "pnl": t.pnl}
                        for t in traders}
        self.wallets_refreshed = now_ts
        self.cursors = {w: c for w, c in self.cursors.items() if w in self.wallets}

    def add_fills(self, fills: list[Fill]) -> int:
        new = 0
        for f in fills:
            if f.side != "BUY" or f.uid in self.seen:
                continue
            self.seen[f.uid] = f.ts
            new += 1
            m = self.markets.setdefault(f.condition_id, {"t": "", "e": "", "o": {}})
            if f.title:
                m["t"] = f.title
            if f.event_slug:
                m["e"] = f.event_slug
            m["o"][str(f.outcome_index)] = f.outcome
            key = f"{f.wallet}|{f.condition_id}|{f.outcome_index}"
            b = self.stakes.setdefault(key, {}).setdefault(
                str(f.ts // BUCKET_S * BUCKET_S), [0.0, 0.0, 0, 0])
            b[0] += f.usd
            b[1] += f.size
            b[2] += 1
            b[3] = max(b[3], f.ts)
        return new

    def mark_alerted(self, key: str, now_ts: int) -> None:
        self.alerted[key] = now_ts

    def prune(self, now_ts: int, window_hours: int) -> None:
        cutoff = now_ts - window_hours * 3600
        for key in list(self.stakes):
            kept = {b: v for b, v in self.stakes[key].items() if int(b) + BUCKET_S > cutoff}
            if kept:
                self.stakes[key] = kept
            else:
                del self.stakes[key]
        live = {k.split("|")[1] for k in self.stakes}
        self.markets = {c: m for c, m in self.markets.items() if c in live}
        self.seen = {u: t for u, t in self.seen.items() if t >= now_ts - SEEN_TTL_S}
        self.alerted = {k: v for k, v in self.alerted.items() if v >= now_ts - ALERT_TTL_S}
