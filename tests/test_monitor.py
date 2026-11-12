from datetime import datetime, timedelta, timezone

import pytest

from polymarket_monitor.api import (PolymarketClient, parse_leaderboard, parse_trade,
                                    parse_trades_page)
from polymarket_monitor.config import Config
from polymarket_monitor.notify import format_cluster, pack_messages, split_message
from polymarket_monitor.runner import run_once
from polymarket_monitor.signals import build_positions as _build
from polymarket_monitor.signals import find_clusters as _clusters, find_large as _large
from polymarket_monitor.state import State
from tests.helpers import FakeClient, FakeNotifier, page, trade_row, traders

NOW = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)
T = int(NOW.timestamp())


def cfg(tmp_path, **kw):
    return Config(state_path=str(tmp_path / "state.json"), **kw)


def fills(*rows):
    return [parse_trade(r) for r in rows]


def find_clusters(pos, c, now_ts=T):
    return _clusters(pos, c, now_ts)


def find_large(pos, c, now_ts=T):
    return _large(pos, c, now_ts)


def build_positions(fl, c, now_ts):
    """Go through State exactly like the runner does (fills -> buckets -> positions)."""
    st = State()
    st.add_fills(fl)
    return _build(st.stakes, st.markets, {}, c, now_ts)


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


def test_prune_drops_old_stakes_and_fill_ids():
    s = State()
    s.add_fills(fills(trade_row("0xa", cond="0xold", ts=T - 40 * 3600),
                      trade_row("0xb", cond="0xnew", ts=T - 60)))
    s.prune(T, 24)
    assert [k.split("|")[1] for k in s.stakes] == ["0xnew"]
    assert list(s.markets) == ["0xnew"] and len(s.seen) == 1


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


def test_same_fill_read_twice_counts_once(tmp_path):
    row = trade_row("0xa", ts=T - 60, price=0.5, size=40000, tx="0xsame")     # $20,000
    st = State()
    assert st.add_fills(fills(row)) == 1
    assert st.add_fills(fills(row)) == 0                    # overlap re-read is ignored
    (p,) = _build(st.stakes, st.markets, {}, cfg(tmp_path), T)
    assert p.usd == 20000 and p.fills == 1


def test_wallet_on_both_sides_is_ignored(tmp_path):
    # 0xa backs Lakers AND Celtics (market-making / hedging); 0xb and 0xc back Lakers
    rows = [trade_row("0xa", ts=T - 600, price=0.5, size=40000),
            trade_row("0xa", ts=T - 600, oi=1, outcome="Celtics", price=0.5, size=40000),
            trade_row("0xb", ts=T - 600, price=0.5, size=2000),
            trade_row("0xc", ts=T - 600, price=0.5, size=2000)]
    pos = build_positions(fills(*rows), cfg(tmp_path), T)
    assert find_large(pos, cfg(tmp_path)) == []              # 0xa's $20k bets don't count
    assert find_clusters(pos, cfg(tmp_path)) == []           # and 0xa doesn't make it 3 wallets
    one_sided = [r for r in rows if r["outcome_index"] == 0]
    assert len(find_clusters(build_positions(fills(*one_sided), cfg(tmp_path), T), cfg(tmp_path))) == 1


def test_state_stays_small_for_a_bot_heavy_day(tmp_path):
    import random
    random.seed(1)
    st = State()
    rows = []
    for w in range(50):                                      # ~15,000 fills in 24h, like the real dry run
        for i in range(300):
            pick = random.randrange(25)
            rows.append(trade_row(f"0x{w:040x}", cond=f"0x{pick:064x}", oi=pick % 2,
                                  ts=T - random.randrange(24 * 3600), size=random.uniform(10, 500),
                                  price=random.uniform(0.2, 0.8), tx=f"0x{w}-{i}"))
    st.add_fills(fills(*rows))
    st.prune(T, 24)
    path = tmp_path / "state.json"
    st.save(str(path))
    assert path.stat().st_size < 2_000_000                   # the per-fill version was ~10 MB


def test_truncation_is_reported(tmp_path):
    class Trunc(FakeClient):
        last_truncated = True
    s = run_once(cfg(tmp_path), Trunc(traders("0xa")), FakeNotifier(), now=NOW)
    assert s["truncated_wallets"] == 1


def test_truncation_flag_set_by_real_client(monkeypatch):
    c = PolymarketClient("test", min_interval=0)
    monkeypatch.setattr(c, "_get_json", lambda path, params: page([trade_row("0xa")], "MORE"))
    c.trades("0xa", start=1, max_pages=2)
    assert c.last_truncated is True
    monkeypatch.setattr(c, "_get_json", lambda path, params: page([trade_row("0xa")]))
    c.trades("0xa", start=1, max_pages=2)
    assert c.last_truncated is False


def test_every_polymarket_message_is_clearly_labelled(tmp_path):
    c, n = cfg(tmp_path), FakeNotifier()
    client = FakeClient(traders(*WALLETS))
    run_once(c, client, n, now=NOW)
    now2 = NOW + timedelta(minutes=10)
    t2 = int(now2.timestamp())
    for w in ("0xa", "0xb", "0xc"):
        client.rows[w] = [trade_row(w, ts=t2 - 60, price=0.5, size=2000)]
    client.rows["0xd"] = [trade_row("0xd", cond="0xbig", ts=t2 - 60, price=0.4, size=50000)]
    run_once(c, client, n, now=now2)
    assert len(n.sent) == 2                      # start message + ONE bundle (cluster + large bet)
    assert all(m.startswith("\U0001F3B2") for m in n.sent)
    assert n.sent[1].count("POLYMARKET") == 2 and "Large bet" in n.sent[1] and "3 top traders" in n.sent[1]


# ---------- time-window clusters, freshness, bundling ----------
def at(wallet, minutes_ago, **kw):
    kw.setdefault("price", 0.5)
    kw.setdefault("size", 2000)
    return trade_row(wallet, ts=T - minutes_ago * 60, **kw)


def clusters_of(rows, tmp_path, **cfg_kw):
    c = cfg(tmp_path, **cfg_kw)
    return find_clusters(build_positions(fills(*rows), c, T), c)


def test_buys_far_apart_in_time_are_not_a_cluster(tmp_path):
    # three traders, but 3 hours apart: a follower could not get a similar price
    assert clusters_of([at("0xa", 200), at("0xb", 100), at("0xc", 10)], tmp_path) == []


def test_only_the_traders_inside_the_window_are_reported(tmp_path):
    rows = [at("0xold", 300), at("0xa", 25), at("0xb", 15), at("0xc", 5)]
    (cl,) = clusters_of(rows, tmp_path)
    assert {p.wallet for p in cl.positions} == {"0xa", "0xb", "0xc"}      # 0xold is hours earlier
    assert (cl.last_ts - cl.first_ts) // 60 == 20


def test_window_length_is_configurable(tmp_path):
    rows = [at("0xa", 40), at("0xb", 25), at("0xc", 5)]                   # spread = 35 min
    assert len(clusters_of(rows, tmp_path)) == 1                          # default 60-min window
    assert clusters_of(rows, tmp_path, cluster_window_min=20) == []


def test_stale_cluster_is_not_alerted(tmp_path):
    fresh = [at(w, 10) for w in ("0xa", "0xb", "0xc")]
    stale = [at(w, 180) for w in ("0xa", "0xb", "0xc")]                   # same trio, 3 hours ago
    assert len(clusters_of(fresh, tmp_path)) == 1
    assert clusters_of(stale, tmp_path) == []
    assert len(clusters_of([at(w, 40) for w in ("0xa", "0xb", "0xc")], tmp_path)) == 1   # < 45 min
    assert clusters_of([at(w, 50) for w in ("0xa", "0xb", "0xc")], tmp_path) == []       # > 45 min


def test_cluster_reports_price_range(tmp_path):
    rows = [at("0xa", 10, price=0.58), at("0xb", 8, price=0.60), at("0xc", 5, price=0.62)]
    (cl,) = clusters_of(rows, tmp_path)
    assert (round(cl.price_low, 2), round(cl.price_high, 2)) == (0.58, 0.62)
    msg = format_cluster(cl, {}, T)
    assert "within 5 min of each other" in msg and "0.58-0.62" in msg


def test_large_bet_must_be_recent_and_concentrated(tmp_path):
    c = cfg(tmp_path)
    one_hour = [at("0xa", 30 - i * 10, size=8000) for i in range(3)]            # $12k inside 60 min
    assert len(find_large(build_positions(fills(*one_hour), c, T), c)) == 1
    spread_out = [at("0xa", 600 - i * 90, size=5000) for i in range(8)]         # $20k over 10 hours
    assert find_large(build_positions(fills(*spread_out), c, T), c) == []
    stale = [at("0xa", 300, size=60000)]                                         # $30k, 5 hours ago
    assert find_large(build_positions(fills(*stale), c, T), c) == []


def test_new_top_wallet_with_old_history_causes_no_alerts(tmp_path):
    c, n = cfg(tmp_path), FakeNotifier()
    client = FakeClient(traders("0xa"))
    run_once(c, client, n, now=NOW)
    client.traders = traders("0xa", "0xb", "0xc", "0xd")                         # leaderboard shuffles
    now2 = NOW + timedelta(hours=7)                                              # past the refresh time
    t2 = int(now2.timestamp())
    for w in ("0xb", "0xc", "0xd"):                                              # their cluster was 5h ago
        client.rows[w] = [trade_row(w, ts=t2 - 5 * 3600, price=0.5, size=40000)]
    s = run_once(c, client, n, now=now2)
    assert s["cluster_alerts"] == 0 and s["large_alerts"] == 0 and len(n.sent) == 1


def test_burst_of_alerts_is_bundled_into_one_message(tmp_path):
    c, n = cfg(tmp_path), FakeNotifier()
    client = FakeClient(traders("0xa", "0xb", "0xc"))
    run_once(c, client, n, now=NOW)
    now2 = NOW + timedelta(minutes=10)
    t2 = int(now2.timestamp())
    for w in ("0xa", "0xb", "0xc"):
        client.rows[w] = [trade_row(w, cond=f"0xgame{g}", title=f"Game {g}", ts=t2 - 120,
                                    price=0.5, size=2000) for g in range(3)]     # 3 picks x 3 traders
    s = run_once(c, client, n, now=now2)
    assert s["cluster_alerts"] == 3 and s["alert_messages"] == 1 and len(n.sent) == 2
    for g in range(3):
        assert f"Game {g}" in n.sent[1]


def test_pack_messages_never_splits_an_alert():
    texts = ["a" * 1500, "b" * 1500, "c" * 1500, "d" * 100]
    groups = pack_messages(texts, limit=3500)
    assert sorted(i for g in groups for i in g) == [0, 1, 2, 3] and len(groups) == 2
    assert pack_messages([]) == []


def test_second_alert_on_same_pick_is_labelled_update(tmp_path):
    c, n = cfg(tmp_path), FakeNotifier()
    client = FakeClient(traders(*WALLETS))
    run_once(c, client, n, now=NOW)
    now2 = NOW + timedelta(minutes=10)
    t2 = int(now2.timestamp())
    for w in ("0xa", "0xb", "0xc"):
        client.rows[w] = [trade_row(w, ts=t2 - 300, price=0.5, size=2000)]
    run_once(c, client, n, now=now2)
    assert "UPDATE" not in n.sent[-1]
    now3 = now2 + timedelta(minutes=10)
    client.rows["0xd"] = [trade_row("0xd", ts=int(now3.timestamp()) - 60, price=0.5, size=2000)]
    s = run_once(c, client, n, now=now3)
    assert s["cluster_alerts"] == 1 and "UPDATE: now 4 top traders" in n.sent[-1]
