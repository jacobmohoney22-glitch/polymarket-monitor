from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .api import Fill
from .config import Config


@dataclass(frozen=True)
class Position:
    """One wallet's combined BUYs on one pick (market + outcome) inside the window."""
    wallet: str
    name: str
    condition_id: str
    outcome_index: int
    outcome: str
    title: str
    event_slug: str
    usd: float
    shares: float
    avg_price: float
    last_ts: int
    fills: int


@dataclass(frozen=True)
class Cluster:
    key: str                       # changes when another wallet joins -> fresh alert
    condition_id: str
    outcome_index: int
    outcome: str
    title: str
    event_slug: str
    positions: tuple[Position, ...]
    total_usd: float
    avg_price: float               # stake-weighted
    opposing_wallets: int
    opposing_usd: float


def build_positions(fills: Iterable[Fill], cfg: Config, now_ts: int) -> list[Position]:
    cutoff = now_ts - cfg.window_hours * 3600
    agg: dict[tuple, dict] = {}
    for f in fills:
        if f.side != "BUY" or f.ts < cutoff:
            continue
        a = agg.setdefault((f.wallet, f.condition_id, f.outcome_index), {
            "name": f.name, "outcome": f.outcome, "title": f.title,
            "event_slug": f.event_slug, "usd": 0.0, "shares": 0.0, "last": 0, "n": 0})
        a["usd"] += f.usd
        a["shares"] += f.size
        a["last"] = max(a["last"], f.ts)
        a["n"] += 1
    out = []
    for (wallet, cond, oi), a in agg.items():
        out.append(Position(wallet, a["name"], cond, oi, a["outcome"], a["title"],
                            a["event_slug"], a["usd"], a["shares"],
                            a["usd"] / a["shares"], a["last"], a["n"]))
    return out


def _eligible(positions: Iterable[Position], cfg: Config) -> list[Position]:
    # buying at 96c+ is usually a near-certain outcome, not a conviction call
    return [p for p in positions if p.avg_price <= cfg.max_price]


def find_large(positions: Iterable[Position], cfg: Config) -> list[Position]:
    return sorted((p for p in _eligible(positions, cfg) if p.usd >= cfg.large_bet_usd),
                  key=lambda p: p.usd, reverse=True)


def find_clusters(positions: Iterable[Position], cfg: Config) -> list[Cluster]:
    eligible = _eligible(positions, cfg)
    by_pick: dict[tuple, list[Position]] = {}
    for p in eligible:
        if p.usd >= cfg.min_wallet_usd:
            by_pick.setdefault((p.condition_id, p.outcome_index), []).append(p)

    clusters = []
    for (cond, oi), plist in by_pick.items():
        total = sum(p.usd for p in plist)
        if len(plist) < cfg.min_wallets or total < cfg.cluster_min_usd:
            continue
        plist = sorted(plist, key=lambda p: p.usd, reverse=True)
        backers = {p.wallet for p in plist}
        opp = [q for q in eligible if q.condition_id == cond and q.outcome_index != oi
               and q.usd >= cfg.min_wallet_usd and q.wallet not in backers]
        shares = sum(p.shares for p in plist)
        clusters.append(Cluster(
            key=f"{cond}:{oi}:{','.join(sorted(backers))}",
            condition_id=cond, outcome_index=oi, outcome=plist[0].outcome,
            title=plist[0].title, event_slug=plist[0].event_slug,
            positions=tuple(plist), total_usd=total, avg_price=total / shares,
            opposing_wallets=len({q.wallet for q in opp}),
            opposing_usd=sum(q.usd for q in opp)))
    clusters.sort(key=lambda c: c.total_usd, reverse=True)
    return clusters
