from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from .config import Config
from .state import BUCKET_S


@dataclass(frozen=True)
class Position:
    """One wallet's combined BUYs on one pick (market + outcome)."""
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
    # (last_fill_ts, usd, shares, fills) per 15-minute bucket, oldest first
    events: tuple = ()


@dataclass(frozen=True)
class Cluster:
    key: str                       # changes when the set of traders changes
    condition_id: str
    outcome_index: int
    outcome: str
    title: str
    event_slug: str
    positions: tuple[Position, ...]
    total_usd: float
    avg_price: float               # stake-weighted
    price_low: float               # cheapest / dearest average price paid by any trader
    price_high: float
    first_ts: int                  # earliest and latest buy inside the cluster window
    last_ts: int
    opposing_wallets: int
    opposing_usd: float


def build_positions(stakes: dict, markets: dict, wallets: dict, cfg: Config,
                    now_ts: int) -> list[Position]:
    """Sum each wallet's bucketed BUYs on each pick over the look-back window."""
    cutoff = now_ts - cfg.window_hours * 3600
    out = []
    for key, buckets in stakes.items():
        wallet, cond, oi_s = key.split("|")
        events = sorted((last_ts, u, sh, n) for b, (u, sh, n, last_ts) in buckets.items()
                        if int(b) + BUCKET_S > cutoff)
        if not events:
            continue
        usd = sum(e[1] for e in events)
        shares = sum(e[2] for e in events)
        if shares <= 0:
            continue
        m = markets.get(cond, {})
        out.append(Position(wallet, wallets.get(wallet, {}).get("name", ""), cond, int(oi_s),
                            m.get("o", {}).get(oi_s, ""), m.get("t", ""), m.get("e", ""),
                            usd, shares, usd / shares, events[-1][0],
                            sum(e[3] for e in events), tuple(events)))
    return out


def _eligible(positions: Iterable[Position], cfg: Config) -> list[Position]:
    positions = list(positions)
    # a wallet holding BOTH sides of a market is hedging / market-making, not expressing a view
    sides: dict[tuple, set] = {}
    for p in positions:
        if p.usd >= cfg.min_wallet_usd:
            sides.setdefault((p.wallet, p.condition_id), set()).add(p.outcome_index)
    two_sided = {k for k, v in sides.items() if len(v) >= 2}
    # buying at 96c+ is usually a near-certain outcome, not a conviction call
    return [p for p in positions
            if p.avg_price <= cfg.max_price and (p.wallet, p.condition_id) not in two_sided]


def find_large(positions: Iterable[Position], cfg: Config, now_ts: int) -> list[Position]:
    """A wallet putting >= large_bet_usd on one pick within one cluster-window, and still fresh."""
    w = cfg.cluster_window_min * 60
    out = []
    for p in _eligible(positions, cfg):
        ev, best, left = p.events, None, 0
        for right in range(len(ev)):
            while ev[right][0] - ev[left][0] > w:
                left += 1
            if sum(e[1] for e in ev[left:right + 1]) >= cfg.large_bet_usd:
                best = (left, right)                  # keep the latest qualifying window
        if best is None:
            continue
        win = ev[best[0]:best[1] + 1]
        if now_ts - win[-1][0] > cfg.max_alert_age_min * 60:
            continue                                  # stale: the bet is hours old
        usd, shares = sum(e[1] for e in win), sum(e[2] for e in win)
        out.append(replace(p, usd=usd, shares=shares, avg_price=usd / shares,
                           last_ts=win[-1][0], fills=sum(e[3] for e in win), events=()))
    return sorted(out, key=lambda p: p.usd, reverse=True)


def find_clusters(positions: Iterable[Position], cfg: Config, now_ts: int) -> list[Cluster]:
    """>= min_wallets distinct top traders on the same pick whose buys all fall inside one
    cluster_window_min window (so a follower can still get a similar price), and whose most
    recent buy is at most max_alert_age_min old."""
    eligible = _eligible(positions, cfg)
    w = cfg.cluster_window_min * 60
    by_pick: dict[tuple, list[Position]] = {}
    for p in eligible:
        by_pick.setdefault((p.condition_id, p.outcome_index), []).append(p)

    clusters = []
    for (cond, oi), plist in by_pick.items():
        by_wallet = {p.wallet: p for p in plist}
        events = sorted((t, p.wallet, u, sh, n) for p in plist for (t, u, sh, n) in p.events)
        best, left = None, 0
        for right in range(len(events)):
            while events[right][0] - events[left][0] > w:
                left += 1
            agg: dict[str, list] = {}
            for t, wallet, u, sh, n in events[left:right + 1]:
                a = agg.setdefault(wallet, [0.0, 0.0, 0, 0, t])   # usd, shares, fills, last, first
                a[0] += u; a[1] += sh; a[2] += n; a[3] = max(a[3], t); a[4] = min(a[4], t)
            strong = {k: v for k, v in agg.items() if v[0] >= cfg.min_wallet_usd}
            total = sum(v[0] for v in strong.values())
            if len(strong) >= cfg.min_wallets and total >= cfg.cluster_min_usd:
                best = strong                              # keep the latest qualifying window
        if best is None:
            continue
        last_ts = max(v[3] for v in best.values())
        first_ts = min(v[4] for v in best.values())
        if now_ts - last_ts > cfg.max_alert_age_min * 60:
            continue                                       # stale: don't alert on old news
        members = tuple(sorted(
            (replace(by_wallet[k], usd=v[0], shares=v[1], avg_price=v[0] / v[1],
                     last_ts=v[3], fills=v[2], events=()) for k, v in best.items()),
            key=lambda p: p.usd, reverse=True))
        opp = [q for q in eligible if q.condition_id == cond and q.outcome_index != oi
               and q.usd >= cfg.min_wallet_usd and q.wallet not in best
               and q.last_ts >= first_ts - w]
        total = sum(p.usd for p in members)
        clusters.append(Cluster(
            key=f"{cond}:{oi}:{','.join(sorted(best))}",
            condition_id=cond, outcome_index=oi, outcome=members[0].outcome,
            title=members[0].title, event_slug=members[0].event_slug, positions=members,
            total_usd=total, avg_price=total / sum(p.shares for p in members),
            price_low=min(p.avg_price for p in members),
            price_high=max(p.avg_price for p in members),
            first_ts=first_ts, last_ts=last_ts,
            opposing_wallets=len({q.wallet for q in opp}),
            opposing_usd=sum(q.usd for q in opp)))
    clusters.sort(key=lambda c: c.total_usd, reverse=True)
    return clusters
