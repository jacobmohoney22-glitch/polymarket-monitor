"""Synthetic API payloads shaped like the documented /v2/trades and /v2/leaderboard responses."""
from __future__ import annotations

import requests

from polymarket_monitor.api import Trader, parse_trade


def trade_row(wallet, *, cond="0xcond1", oi=0, outcome="Lakers", price=0.50, size=2000.0,
              ts=1_000_000, title="Lakers vs Celtics", side="BUY", tx=None, name="Whale",
              event_slug="lakers-celtics"):
    return {"proxy_wallet": wallet, "side": side, "token_id": f"tok-{cond}-{oi}",
            "condition_id": cond, "size": size, "price": price, "timestamp": ts,
            "title": title, "slug": "lakers-celtics-winner", "icon": "", "event_slug": event_slug,
            "outcome": outcome, "outcome_index": oi, "name": name, "pseudonym": "x", "bio": "",
            "profile_image": "", "profile_image_optimized": "",
            "transaction_hash": tx or f"0x{wallet}{cond}{oi}{ts}{size}"}


def page(rows, next_cursor=None):
    return {"data": rows, "pagination": {"limit": 500, "offset": 0,
                                         "has_more": next_cursor is not None,
                                         "next_cursor": next_cursor}}


def traders(*wallets):
    return [Trader(w, f"Trader {w}", i + 1, 10_000.0 * (10 - i)) for i, w in enumerate(wallets)]


class FakeClient:
    def __init__(self, trader_list, rows_by_wallet=None, fail=()):
        self.traders, self.rows, self.fail = trader_list, rows_by_wallet or {}, set(fail)
        self.lb_calls, self.trade_calls = 0, []

    def leaderboard(self, category, time_period, limit):
        self.lb_calls += 1
        return self.traders

    def trades(self, wallet, start, max_pages=3):
        self.trade_calls.append((wallet, start))
        if wallet in self.fail:
            raise requests.ConnectionError("boom")
        fills = [parse_trade(r) for r in self.rows.get(wallet, [])]
        return [f for f in fills if f and f.ts >= start and f.side == "BUY"]


class FakeNotifier:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def send(self, text):
        self.sent.append(text)
        return self.ok
