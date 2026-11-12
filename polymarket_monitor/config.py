from __future__ import annotations

import os
from dataclasses import dataclass

WINDOWS = ("day", "week", "month", "all")


def _get(env, key, default):
    value = env.get(key)
    return default if value is None or str(value).strip() == "" else str(value).strip()


@dataclass(frozen=True)
class Config:
    # who to follow
    category: str = "sports"
    leaderboard_window: str = "month"   # day | week | month | all
    top_n: int = 50
    refresh_hours: int = 6              # how often to re-read the leaderboard
    # what counts as a signal
    window_hours: int = 24              # bets older than this are ignored
    min_wallets: int = 3                # distinct top traders on the same pick
    cluster_window_min: int = 60        # ...who bought within this many minutes of each other
    max_alert_age_min: int = 45         # only alert if the latest buy is at most this old
    min_wallet_usd: float = 250.0       # a wallet's stake must be at least this to count
    cluster_min_usd: float = 3_000.0    # combined stake across the cluster
    large_bet_usd: float = 10_000.0     # one wallet, one pick
    max_price: float = 0.95             # ignore ~sure-thing buys (avg price above this)
    max_pages: int = 6
    # delivery / storage
    telegram_token: str = ""
    telegram_chat_id: str = ""
    state_path: str = "state/state.json"
    contact: str = ""                   # optional, added to the User-Agent

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env
        g = lambda k, d: _get(env, k, d)
        window = g("LEADERBOARD_WINDOW", "month").lower()
        if window not in WINDOWS:
            raise SystemExit(f"LEADERBOARD_WINDOW must be one of {WINDOWS}, got {window!r}")
        return cls(
            category=g("LEADERBOARD_CATEGORY", "sports").lower(),
            leaderboard_window=window,
            top_n=max(1, min(1000, int(g("TOP_N", 50)))),
            refresh_hours=int(g("REFRESH_HOURS", 6)),
            window_hours=int(g("WINDOW_HOURS", 24)),
            min_wallets=int(g("MIN_WALLETS", 3)),
            cluster_window_min=int(g("CLUSTER_WINDOW_MIN", 60)),
            max_alert_age_min=int(g("MAX_ALERT_AGE_MIN", 45)),
            min_wallet_usd=float(g("MIN_WALLET_USD", 250)),
            cluster_min_usd=float(g("CLUSTER_MIN_USD", 3000)),
            large_bet_usd=float(g("LARGE_BET_USD", 10000)),
            max_price=float(g("MAX_PRICE", 0.95)),
            max_pages=int(g("MAX_PAGES", 6)),
            telegram_token=g("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=g("TELEGRAM_CHAT_ID", ""),
            state_path=g("STATE_PATH", "state/state.json"),
            contact=g("CONTACT", ""),
        )

    @property
    def user_agent(self) -> str:
        return "polymarket-monitor/1.0" + (f" ({self.contact})" if self.contact else "")

    def require_telegram(self) -> None:
        if not (self.telegram_token and self.telegram_chat_id):
            raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required "
                             "(or use --dry-run).")
