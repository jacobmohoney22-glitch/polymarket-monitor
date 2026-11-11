from __future__ import annotations

import html

import requests

from .signals import Cluster, Position

TELEGRAM_LIMIT = 4096


def _esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def _usd(v: float) -> str:
    return f"${v:,.0f}"


def _ago(ts: int, now_ts: int) -> str:
    m = max(0, (now_ts - ts) // 60)
    return f"{m}m ago" if m < 90 else f"{m // 60}h ago"


def _who(p: Position, traders: dict) -> str:
    t = traders.get(p.wallet, {})
    name = t.get("name") or p.name or f"{p.wallet[:6]}...{p.wallet[-4:]}"
    return f"{_esc(name)} (#{t['rank']})" if t.get("rank") else _esc(name)


def _link(event_slug: str) -> str:
    if not event_slug:
        return ""
    url = html.escape(f"https://polymarket.com/event/{event_slug}", quote=True)
    return f'\n<a href="{url}">View market</a>'


def format_cluster(c: Cluster, traders: dict, now_ts: int) -> str:
    lines = [f"<b>[Polymarket] {len(c.positions)} top traders on the same pick</b>",
             f"{_esc(c.title)}",
             f"Pick: <b>{_esc(c.outcome)}</b>  |  combined {_usd(c.total_usd)} "
             f"at avg price {c.avg_price:.2f}", ""]
    for p in c.positions:
        lines.append(f"• {_who(p, traders)}: {_usd(p.usd)} @ {p.avg_price:.2f} "
                     f"({p.fills} fill{'s' if p.fills != 1 else ''}, last {_ago(p.last_ts, now_ts)})")
    if c.opposing_wallets:
        lines += ["", f"Other side: {c.opposing_wallets} top trader"
                      f"{'s' if c.opposing_wallets != 1 else ''}, {_usd(c.opposing_usd)}"]
    return "\n".join(lines) + _link(c.event_slug)


def format_large(p: Position, traders: dict, now_ts: int) -> str:
    return (f"<b>[Polymarket] Large bet by a top trader</b>\n"
            f"{_who(p, traders)}: {_usd(p.usd)} @ {p.avg_price:.2f} "
            f"({p.fills} fill{'s' if p.fills != 1 else ''}, last {_ago(p.last_ts, now_ts)})\n"
            f"{_esc(p.title)}\nPick: <b>{_esc(p.outcome)}</b>") + _link(p.event_slug)


def split_message(text: str, limit: int = TELEGRAM_LIMIT - 100) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = limit if cut <= 0 else cut
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


class ConsoleNotifier:
    def send(self, text: str) -> bool:
        print("---- ALERT ----\n" + text + "\n")
        return True


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.session = session or requests.Session()

    def send(self, text: str) -> bool:
        ok = True
        for chunk in split_message(text):
            try:
                r = self.session.post(self.url, timeout=20, json={
                    "chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                    "disable_web_page_preview": True})
                if r.status_code != 200:
                    ok = False
                    print(f"telegram send failed: HTTP {r.status_code} {r.text[:200]}")
            except requests.RequestException as e:
                ok = False
                print(f"telegram send failed: {type(e).__name__}")   # never print the URL (token)
        return ok
