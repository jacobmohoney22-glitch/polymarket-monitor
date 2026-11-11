from __future__ import annotations

from datetime import datetime, timezone

import requests

from .config import Config
from .notify import format_cluster, format_large
from .signals import build_positions, find_clusters, find_large
from .state import State

OVERLAP_S = 900   # re-read the last 15 min each run: absorbs indexing lag; uid dedupe handles repeats


def run_once(cfg: Config, client, notifier, now: datetime | None = None,
             persist: bool = True) -> dict:
    now = now or datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    state = State.load(cfg.state_path)
    first_run = state.is_new
    summary = {"first_run": first_run, "wallets": 0, "new_fills": 0, "fetch_errors": 0,
               "cluster_alerts": 0, "large_alerts": 0}

    # 1. who are we following? (refresh the leaderboard every few hours)
    if not state.wallets or now_ts - state.wallets_refreshed >= cfg.refresh_hours * 3600:
        traders = client.leaderboard(cfg.category, cfg.leaderboard_window, cfg.top_n)
        if traders:
            state.set_wallets(traders, now_ts)
        elif not state.wallets:
            raise SystemExit("Leaderboard returned no traders; nothing to follow.")
    summary["wallets"] = len(state.wallets)

    # 2. new BUY fills from each followed wallet
    window_start = now_ts - cfg.window_hours * 3600
    for wallet in list(state.wallets):
        since = max(state.cursors.get(wallet, 0) - OVERLAP_S, window_start)
        try:
            fills = client.trades(wallet, since, cfg.max_pages)
        except requests.RequestException:
            summary["fetch_errors"] += 1
            continue                      # cursor not advanced -> retried next run
        summary["new_fills"] += state.add_fills(fills)
        state.cursors[wallet] = now_ts

    # 3. signals
    positions = build_positions(state.all_fills(), cfg, now_ts)
    pending = [(f"large:{p.wallet}:{p.condition_id}:{p.outcome_index}",
                format_large(p, state.wallets, now_ts), "large_alerts")
               for p in find_large(positions, cfg)]
    pending += [(f"cluster:{c.key}", format_cluster(c, state.wallets, now_ts), "cluster_alerts")
                for c in find_clusters(positions, cfg)]

    for key, text, counter in pending:
        if key in state.alerted:
            continue
        if first_run:
            state.mark_alerted(key, now_ts)    # baseline: don't blast what already happened
        elif notifier.send(text):
            state.mark_alerted(key, now_ts)    # only after a successful send
            summary[counter] += 1

    if first_run:
        state.started = now.isoformat()
        notifier.send(f"[Polymarket] Monitor started. Following the top {summary['wallets']} "
                      f"{cfg.category} traders ({cfg.leaderboard_window} PnL). Baseline loaded from "
                      f"{summary['new_fills']} recent buys; alerts begin with the next run.")

    state.prune(now_ts, cfg.window_hours)
    if persist:
        state.save(cfg.state_path)
    return summary
