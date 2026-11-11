from datetime import datetime, timedelta, timezone

import pytest

from polymarket_monitor.api import (PolymarketClient, parse_leaderboard, parse_trade,
                                    parse_trades_page)
from polymarket_monitor.config import Config
from polymarket_monitor.notify import format_cluster, split_message
from polymarket_monitor.runner import run_once
from polymarket_monitor.signals import build_positions, find_clusters, find_large
from polymarket_monitor.state import State
from tests.helpers import FakeClient, FakeNotifier, page, trade_row, traders

NOW = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)
T = int(NOW.timestamp())


def cfg(tmp_path, **kw):
    return Config(state_path=str(tmp_path / "state.json"), **kw)


def fills(*rows):
    return [parse_trade(r) for r in rows]


# ---------- parsing ----------
def test_parse_trade_and_usd():
    f = parse_trade(trade_row("0xa", price=0.40, size=500, ts=T))
    assert (f.wallet, f.side, f.outcome, f.usd) == ("0xa", "BUY", "Lakers", 200.0)


def test_parse_trade_skips_unusable_rows():
    assert parse_trade({"proxy_wallet": "0xa"}) is None
    assert parse_trade(trade_row("0xa", size=0)) is None
    bad = trade_row("0xa"); bad["timestamp"] = "soon"
    assert parse_trade(bad) is None


def test_unlabeled_outcome_index_999():
    row = trade_row("0xa"); row["outcome_index"] = None
    assert parse_trade(row).outcome_index == 999


def test_parse_leaderboard():
    payload = {"data": [{"rank": 1, "user_id": "0xABC", "pnl": 123.5, "volume": 9,
                         "user_name": "Ace", "profile_image": "", "x_username": "", "verified": False}],
               "pagination": {}}
    (t,) = parse_leaderboard(payload)
    assert (t.wallet, t.name, t.rank, t.pnl) == ("0xabc", "Ace", 1, 123.5)


def test_pagination_cursor_only_when_has_more():
    fills_, cur = parse_trades_page(page([trade_row("0xa")], next_cursor="CUR"))
    assert len(fills_) == 1 and cur == "CUR"
    assert parse_trades_page(page([trade_row("0xa")]))[1] is None


def test_client_follows_cursor_and_resends_filters(monkeypatch):
    c = PolymarketClient("test", min_interval=0)
    calls = []
    pages = iter([page([trade_row("0xa", ts=1)], "C1"), page([trade_row("0xa", ts=2)], None)])
    monkeypatch.setattr(c, "_get_json", lambda path, params: (calls.append(dict(params)), next(pages))[1])
    out = c.trades("0xa", start=123)
    assert len(out) == 2 and "cursor" not in calls[0] and calls[1]["cursor"] == "C1"
    for p in calls:      # identical filters on every page
        assert (p["user"], p["side"], p["start"], p["taker_only"]) == ("0xa", "BUY", 123, "false")


# ---------- signals ----------
def test_fills_merge_into_one_position_with_weighted_price(tmp_path):
    fl = fills(trade_row("0xa", price=0.40, size=1000, ts=T - 600, tx="1"),
               trade_row("0xa", price=0.60, size=1000, ts=T - 300, tx="2"))
    (p,) = build_positions(fl, cfg(tmp_path), T)
    assert p.usd == 1000 and p.fills == 2 and round(p.avg_price, 2) == 0.50


def test_old_fills_and_sells_are_ignored(tmp_path):
    fl = fills(trade_row("0xa", ts=T - 30 * 3600), trade_row("0xb", side="SELL", ts=T - 60))
    assert build_positions(fl, cfg(tmp_path), T) == []


def test_large_bet(tmp_path):
    pos = build_positions(fills(trade_row("0xa", price=0.5, size=30000, ts=T - 60)), cfg(tmp_path), T)
    assert len(find_large(pos, cfg(tmp_path))) == 1          # $15,000 >= $10,000
    small = build_positions(fills(trade_row("0xa", price=0.5, size=10000, ts=T - 60)), cfg(tmp_path), T)
    assert find_large(small, cfg(tmp_path)) == []            # $5,000


def test_near_certain_buys_are_filtered(tmp_path):
    pos = build_positions(fills(trade_row("0xa", price=0.98, size=20000, ts=T - 60)), cfg(tmp_path), T)
    assert find_large(pos, cfg(tmp_path)) == []


def three(**over):
    return [trade_row(w, ts=T - 600, price=0.5, size=2000, **over) for w in ("0xa", "0xb", "0xc")]


def test_three_wallets_same_pick_is_a_cluster(tmp_path):
    pos = build_positions(fills(*three()), cfg(tmp_path), T)
    (c,) = find_clusters(pos, cfg(tmp_path))
    assert len(c.positions) == 3 and c.total_usd == 3000 and c.opposing_wallets == 0


@pytest.mark.parametrize("rows", [
    three()[:2],                                                        # only two wallets
    [trade_row(w, ts=T - 600, price=0.5, size=100) for w in ("0xa", "0xb", "0xc")],   # $50 each: dust
    [trade_row("0xa", ts=T - 600), trade_row("0xb", ts=T - 600),
     trade_row("0xc", ts=T - 600, oi=1, outcome="Celtics")],            # split across outcomes
    [trade_row(w, ts=T - 600, price=0.97, size=2000) for w in ("0xa", "0xb", "0xc")],  # sure thing
])
def test_non_clusters(tmp_path, rows):
    assert find_clusters(build_positions(fills(*rows), cfg(tmp_path), T), cfg(tmp_path)) == []


def test_opposing_side_is_reported(tmp_path):
    rows = three() + [trade_row("0xd", ts=T - 300, oi=1, outcome="Celtics", price=0.5, size=1000)]
    (c,) = find_clusters(build_positions(fills(*rows), cfg(tmp_path), T), cfg(tmp_path))
    assert (c.opposing_wallets, c.opposing_usd) == (1, 500.0)
    assert "Other side: 1 top trader" in format_cluster(c, {}, T)


def test_new_wallet_changes_cluster_key(tmp_path):
    a = find_clusters(build_positions(fills(*three()), cfg(tmp_path), T), cfg(tmp_path))[0]
    four = three() + [trade_row("0xd", ts=T - 60, price=0.5, size=2000)]
    b = find_clusters(build_positions(fills(*four), cfg(tmp_path), T), cfg(tmp_path))[0]
    assert a.key != b.key


# ---------- end to end ----------
WALLETS = ("0xa", "0xb", "0xc", "0xd")


def test_end_to_end_baseline_alert_once_restart(tmp_path):
    c, n = cfg(tmp_path), FakeNotifier()
    client = FakeClient(traders(*WALLETS), {"0xd": [trade_row("0xd", cond="0xold", ts=T - 3000,
                                                            price=0.5, size=40000)]})   # $20k, pre-existing
    s1 = run_once(c, client, n, now=NOW)
    assert s1["first_run"] and len(n.sent) == 1 and "Monitor started" in n.sent[0]   # baseline: silent

    now2 = NOW + timedelta(minutes=10)
    t2 = int(now2.timestamp())
    for w in ("0xa", "0xb", "0xc"):
        client.rows[w] = [trade_row(w, ts=t2 - 120, price=0.5, size=2000)]
    s2 = run_once(c, client, n, now=now2)
    assert s2["cluster_alerts"] == 1 and s2["large_alerts"] == 0   # old $20k bet not re-alerted
    assert "3 top traders" in n.sent[-1] and "Lakers" in n.sent[-1]

    before = len(n.sent)                                            # fresh process, same state file
    s3 = run_once(c, client, n, now=now2 + timedelta(minutes=10))
    assert s3["cluster_alerts"] == 0 and len(n.sent) == before


def test_large_bet_alert_after_baseline(tmp_path):
    c, n = cfg(tmp_path), FakeNotifier()
    client = FakeClient(traders(*WALLETS))
    run_once(c, client, n, now=NOW)
    now2 = NOW + timedelta(minutes=10)
    client.rows["0xa"] = [trade_row("0xa", ts=int(now2.timestamp()) - 60, price=0.4, size=50000)]
    s = run_once(c, client, n, now=now2)
    assert s["large_alerts"] == 1 and "Large bet" in n.sent[-1] and "$20,000" in n.sent[-1]


def test_fetch_error_keeps_cursor_and_retries(tmp_path):
    c = cfg(tmp_path)
    client = FakeClient(traders("0xa", "0xb"), fail=("0xa",))
    s = run_once(c, client, FakeNotifier(), now=NOW)
    assert s["fetch_errors"] == 1
    st = State.load(c.state_path)
    assert "0xa" not in st.cursors and "0xb" in st.cursors


def test_cursor_overlap_and_window_floor(tmp_path):
    c = cfg(tmp_path)
    client = FakeClient(traders("0xa"))
    run_once(c, client, FakeNotifier(), now=NOW)
    run_once(c, client, FakeNotifier(), now=NOW + timedelta(minutes=10))
    (_, first), (_, second) = client.trade_calls
    assert first == T - 24 * 3600                         # first run: look back one window
    assert second == T - 900                              # then: previous fetch time minus 15-min overlap


def test_leaderboard_refreshes_only_on_schedule(tmp_path):
    c = cfg(tmp_path)
    client = FakeClient(traders("0xa"))
    run_once(c, client, FakeNotifier(), now=NOW)
    run_once(c, client, FakeNotifier(), now=NOW + timedelta(hours=1))
    assert client.lb_calls == 1
    run_once(c, client, FakeNotifier(), now=NOW + timedelta(hours=7))
    assert client.lb_calls == 2


def test_failed_send_is_retried(tmp_path):
    c = cfg(tmp_path)
    client = FakeClient(traders(*WALLETS))
    run_once(c, client, FakeNotifier(), now=NOW)
    now2 = NOW + timedelta(minutes=10)
    for w in ("0xa", "0xb", "0xc"):
        client.rows[w] = [trade_row(w, ts=int(now2.timestamp()) - 60, price=0.5, size=2000)]
    assert run_once(c, client, FakeNotifier(ok=False), now=now2)["cluster_alerts"] == 0
    assert run_once(c, client, FakeNotifier(), now=now2 + timedelta(minutes=10))["cluster_alerts"] == 1


def test_empty_leaderboard_with_no_wallets_stops(tmp_path):
    with pytest.raises(SystemExit):
        run_once(cfg(tmp_path), FakeClient([]), FakeNotifier(), now=NOW)


def test_corrupt_state_not_silently_reset(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{nope")
    with pytest.raises(SystemExit):
        State.load(str(p))


def test_prune_drops_old_fills(tmp_path):
    s = State()
    s.add_fills(fills(trade_row("0xa", ts=T - 40 * 3600), trade_row("0xb", ts=T - 60)))
    s.prune(T, 24)
    assert len(s.fills) == 1


def test_split_message_limit():
    assert all(len(c) <= 4096 for c in split_message("\n".join("x" * 90 for _ in range(200))))


def test_config_rejects_bad_window():
    with pytest.raises(SystemExit):
        Config.from_env({"LEADERBOARD_WINDOW": "year"})


def test_dust_wallet_does_not_fill_out_a_cluster(tmp_path):
    # two real stakes ($1,500 each = $3,000, enough dollars) plus one $50 wallet:
    # only passes the 3-wallet rule if the dust stake is wrongly counted
    rows = [trade_row("0xa", ts=T - 600, price=0.5, size=3000),
            trade_row("0xb", ts=T - 600, price=0.5, size=3000),
            trade_row("0xc", ts=T - 600, price=0.5, size=100)]
    assert find_clusters(build_positions(fills(*rows), cfg(tmp_path), T), cfg(tmp_path)) == []
    rows[2] = trade_row("0xc", ts=T - 600, price=0.5, size=1000)      # $500: a real stake
    assert len(find_clusters(build_positions(fills(*rows), cfg(tmp_path), T), cfg(tmp_path))) == 1
