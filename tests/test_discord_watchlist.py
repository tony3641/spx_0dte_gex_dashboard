from types import SimpleNamespace
from watchlist_store import WatchlistEntry
from app_state import create_app_state
from discord_bot import _watchlist_contract, _quote_from_stream, _fmt_watchlist, watchlist_view
from watchlist_store import WatchlistStore


class _Stream:
    def __init__(self, bid=None, ask=None, last=None, close=None):
        self.bid, self.ask, self.last, self.close = bid, ask, last, close


def test_watchlist_contract_stk_and_ind():
    from ibapi.contract import Contract
    stk = _watchlist_contract(WatchlistEntry(symbol="QQQ", sec_type="STK", exchange="ARCA"))
    assert isinstance(stk, Contract)
    assert stk.symbol == "QQQ" and stk.secType == "STK" and stk.exchange == "ARCA" and stk.currency == "USD"
    ind = _watchlist_contract(WatchlistEntry(symbol="SPX", sec_type="IND"))
    assert ind.secType == "IND" and ind.exchange == "SMART"


def test_quote_price_prefers_last_then_mid_then_close():
    assert _quote_from_stream(_Stream(last=100.0, close=99.0))["price"] == 100.0
    mid = _quote_from_stream(_Stream(bid=10.0, ask=11.0, close=10.0))
    assert mid["price"] == 10.5
    c = _quote_from_stream(_Stream(close=10.0))
    assert c["price"] == 10.0


def test_quote_change_vs_close():
    q = _quote_from_stream(_Stream(last=105.5, close=100.0))
    assert q["chg"] == 5.5
    assert round(q["chg_pct"], 2) == 5.5


def test_quote_missing_close_yields_dash():
    q = _quote_from_stream(_Stream(last=50.0, close=None))
    assert q["price"] == 50.0 and q["chg"] is None and q["chg_pct"] is None


def test_fmt_watchlist_no_emoji_and_columns():
    rows = [("Main", "SPX", {"price": 6100.5, "chg": 12.3, "chg_pct": 0.2}),
            ("Main", "SPY", {"price": 5987.2, "chg": -9.1, "chg_pct": -0.15})]
    out = _fmt_watchlist(rows, show_list=True)
    assert "list" in out and "price" in out and "chg%" in out
    assert "🟢" not in out and "⚫" not in out
    assert "6,100.50" in out and "+12.30" in out and "+0.20" in out


def test_fmt_watchlist_no_list_column_when_named():
    rows = [("Main", "SPX", {"price": 100.0, "chg": 1.0, "chg_pct": 1.0})]
    assert "list" not in _fmt_watchlist(rows, show_list=False)


def test_watchlist_view_selects_named_and_all(monkeypatch, tmp_path):
    store = WatchlistStore(path=tmp_path / "w.json")
    store.load()
    store.create("Main"); store.add_entry("Main", WatchlistEntry(symbol="SPX", sec_type="IND"))
    store.create("Aux"); store.add_entry("Aux", WatchlistEntry(symbol="QQQ", sec_type="STK"))
    state = create_app_state()
    state.watchlist_store = store

    named = watchlist_view(state, "Main")
    assert named["ok"] is True and named["groups"][0]["name"] == "Main"
    assert named["groups"][0]["entries"][0]["symbol"] == "SPX"

    allv = watchlist_view(state)
    assert [g["name"] for g in allv["groups"]] == ["Main", "Aux"]

    miss = watchlist_view(state, "Nope")
    assert miss["ok"] is False and "Nope" in miss["error"]
