from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter, Retry

BASE = "https://data-api.polymarket.com/v2"


@dataclass(frozen=True)
class Trader:
    wallet: str
    name: str
    rank: int
    pnl: float


@dataclass(frozen=True)
class Fill:
    uid: str
    wallet: str
    name: str
    side: str
    condition_id: str
    token_id: str
    outcome: str
    outcome_index: int
    title: str
    slug: str
    event_slug: str
    price: float
    size: float        # shares
    usd: float         # size * price
    ts: int            # epoch seconds
    tx: str


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_leaderboard(payload: dict) -> list[Trader]:
    rows = payload.get("data") or []
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        wallet = str(r.get("user_id") or "").lower()
        if not wallet:
            continue
        name = r.get("user_name") or r.get("x_username") or ""
        out.append(Trader(wallet, name, int(r.get("rank") or 0), _f(r.get("pnl")) or 0.0))
    return out


def parse_trade(row: dict) -> Fill | None:
    """One /v2/trades row -> Fill. Returns None for rows we can't use."""
    wallet = str(row.get("proxy_wallet") or "").lower()
    cond = str(row.get("condition_id") or "")
    price, size = _f(row.get("price")), _f(row.get("size"))
    try:
        ts = int(row.get("timestamp"))
    except (TypeError, ValueError):
        return None
    if not (wallet and cond) or price is None or not size or size <= 0:
        return None
    side = str(row.get("side") or "").upper()
    token = str(row.get("token_id") or "")
    tx = str(row.get("transaction_hash") or "")
    try:
        oi = int(row.get("outcome_index"))
    except (TypeError, ValueError):
        oi = 999
    uid = hashlib.sha1(f"{tx}|{token}|{wallet}|{side}|{size}|{price}|{ts}".encode()).hexdigest()[:20]
    return Fill(uid, wallet, row.get("name") or row.get("pseudonym") or "", side, cond, token,
                str(row.get("outcome") or ""), oi, str(row.get("title") or ""),
                str(row.get("slug") or ""), str(row.get("event_slug") or ""),
                price, size, size * price, ts, tx)


def parse_trades_page(payload: dict) -> tuple[list[Fill], str | None]:
    fills = [f for f in (parse_trade(r) for r in (payload.get("data") or [])) if f]
    pag = payload.get("pagination") or {}
    return fills, (pag.get("next_cursor") if pag.get("has_more") else None)


class PolymarketClient:
    """Public Polymarket Data API v2 (no key). Throttled, retries 429/5xx and honors Retry-After."""

    def __init__(self, user_agent: str, min_interval: float = 0.2):
        self.min_interval = min_interval
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        retries = Retry(total=3, backoff_factor=1.5,
                        status_forcelist=(429, 500, 502, 503, 504),
                        allowed_methods=("GET",))
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

    def _get_json(self, path: str, params: dict) -> dict:
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        resp = self.session.get(BASE + path, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def leaderboard(self, category: str, time_period: str, limit: int) -> list[Trader]:
        return parse_leaderboard(self._get_json("/leaderboard", {
            "category": category, "time_period": time_period,
            "sort_by": "PNL", "limit": limit}))

    def trades(self, wallet: str, start: int, max_pages: int = 3) -> list[Fill]:
        """BUY fills for one wallet since `start` (epoch s). Filters are re-sent on every
        page, as the API requires for keyset feeds. taker_only=false so limit-order
        (maker) fills are included too."""
        params = {"user": wallet, "side": "BUY", "start": start,
                  "limit": 500, "taker_only": "false"}
        out, cursor = [], None
        for _ in range(max_pages):
            page = dict(params)
            if cursor:
                page["cursor"] = cursor
            fills, cursor = parse_trades_page(self._get_json("/trades", page))
            out.extend(fills)
            if not cursor:
                break
        return out
