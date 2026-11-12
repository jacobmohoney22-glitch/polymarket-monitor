from __future__ import annotations

from datetime import datetime, timezone

import requests

from .config import Config
from .notify import SEPARATOR, format_cluster, format_large, pack_messages
from .signals import build_positions, find_clusters, find_large
from .state import SEEN_TTL_S, State

OVERLAP_S = 900   # re-read the last 15 min each run: absorbs indexing lag; uid dedupe handles repeats


def run_once(cfg: Config, client, notifier, now: datetime | None = None,
             persist: bool = True) -> dict:
    now = now or datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    state = State.load(cfg.state_path)
    first_run = state.is_new
    summary = {"first_run": first_run, "wallets": 0, "new_fills": 0, "fetch_errors": 0,
               "cluster_alerts": 0, "large_alerts": 0, "alert_messages": 0,
               "truncated_wallets": 0}

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
        cursor = state.cursors.get(wallet)
        if cursor is None:
            since = window_start                 # new wallet: look back one full window
        else:                                    # never re-read past what fill-id memory covers
            since = max(cursor - OVERLAP_S, window_start, now_ts - (SEEN_TTL_S - 3600))
        try:
            fills = client.trades(wallet, since, cfg.max_pages)
        except requests.RequestException:
            summary["fetch_errors"] += 1
            continue                      # cursor not advanced -> retried next run
        summary["new_fills"] += state.add_fills(fills)
        if getattr(client, "last_truncated", False):
            summary["truncated_wallets"] += 1
        state.cursors[wallet] = now_ts

    # 3. signals (only fresh ones: stale news is never alerted)
    positions = build_positions(state.stakes, state.markets, state.wallets, cfg, now_ts)
    pending = []                                          # (sort_usd, key, text, counter)
    for p in find_large(positions, cfg, now_ts):
        key = f"large:{p.wallet}:{p.condition_id}:{p.outcome_index}"
        if key not in state.alerted:
            pending.append((p.usd, key, format_large(p, state.wallets, now_ts), "large_alerts"))
    for c in find_clusters(positions, cfg, now_ts):
        key = f"cluster:{c.key}"
        if key in state.alerted:
            continue
        prefix = f"cluster:{c.condition_id}:{c.outcome_index}:"
        update = any(k.startswith(prefix) for k in state.alerted)   # same pick alerted before
        pending.append((c.total_usd, key,
                        format_cluster(c, state.wallets, now_ts, update), "cluster_alerts"))
    pending.sort(key=lambda x: x[0], reverse=True)

    if first_run:
        for _, key, _, _ in pending:
            state.mark_alerted(key, now_ts)    # baseline: don't blast what already happened
    else:
        texts = [t for _, _, t, _ in pending]
        for group in pack_messages(texts):     # one notification per bundle, not per alert
            if notifier.send(SEPARATOR.join(texts[i] for i in group)):
                summary["alert_messages"] += 1
                for i in group:                # only mark what was actually delivered
                    _, key, _, counter = pending[i]
                    state.mark_alerted(key, now_ts)
                    summary[counter] += 1

    if first_run:
        state.started = now.isoformat()
        notifier.send(f"\U0001F3B2 POLYMARKET \u00b7 Monitor started. Following the top "
                      f"{summary['wallets']} {cfg.category} traders ({cfg.leaderboard_window} PnL). "
                      f"Baseline loaded from {summary['new_fills']} recent buys; alerts begin "
                      f"with the next run.")

    state.prune(now_ts, cfg.window_hours)
    if persist:
        state.save(cfg.state_path)
    return summary
