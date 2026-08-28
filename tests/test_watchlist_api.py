import asyncio

import pytest
from starlette.requests import Request

import server
from watchlist_store import WatchlistStore, WatchlistEntry


def _req(host="127.0.0.1"):
    return Request({"type": "http", "client": (host, 5000)})


def _store(tmp_path):
    s = WatchlistStore(path=tmp_path / "w.json")
    s.load()
    s.create("Main")
    s.add_entry("Main", WatchlistEntry(symbol="SPX", sec_type="IND"))
    return s


def _run(coro):
    return asyncio.run(coro)


def test_get_watchlist_returns_store(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "watchlist_store", _store(tmp_path))
    out = _run(server.get_watchlist(_req()))
    names = [w["name"] for w in out["watchlists"]]
    assert names == ["Main"]
    assert out["watchlists"][0]["entries"][0]["symbol"] == "SPX"


def test_get_watchlist_forbidden_off_localhost(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "watchlist_store", _store(tmp_path))
    with pytest.raises(server.HTTPException) as exc:
        _run(server.get_watchlist(_req(host="8.8.8.8")))
    assert exc.value.status_code == 403


def test_post_watchlist_replaces_and_saves(monkeypatch, tmp_path):
    store = _store(tmp_path)
    monkeypatch.setattr(server, "watchlist_store", store)
    body = server.WatchlistSetIn(watchlists=[
        {"name": "Aux", "entries": [{"symbol": "QQQ", "sec_type": "STK"}]}])
    out = _run(server.post_watchlist(body, _req()))
    assert [w["name"] for w in out["watchlists"]] == ["Aux"]
    assert server.watchlist_store.get("Main") is None
    # state.watchlist_store must track the swap, not go stale.
    assert server.state.watchlist_store.get("Main") is None


def test_post_watchlist_coerces_unknown_sec_type(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "watchlist_store", _store(tmp_path))
    body = server.WatchlistSetIn(watchlists=[
        {"name": "Bad", "entries": [{"symbol": "AAPL", "sec_type": "FUT"}]}])
    out = _run(server.post_watchlist(body, _req()))
    entry = out["watchlists"][0]["entries"][0]
    assert entry["sec_type"] == "STK"     # unknown type coerced, not rejected


def test_post_watchlist_rejects_empty_name(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "watchlist_store", _store(tmp_path))
    body = server.WatchlistSetIn(watchlists=[{"name": "  ", "entries": []}])
    with pytest.raises(server.HTTPException) as exc:
        _run(server.post_watchlist(body, _req()))
    assert exc.value.status_code == 400


def test_post_watchlist_rejects_duplicate_symbol_in_list(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "watchlist_store", _store(tmp_path))
    body = server.WatchlistSetIn(watchlists=[
        {"name": "Dup", "entries": [{"symbol": "AAPL", "sec_type": "STK"},
                                    {"symbol": "aapl", "sec_type": "STK"}]}])
    with pytest.raises(server.HTTPException) as exc:
        _run(server.post_watchlist(body, _req()))
    assert exc.value.status_code == 400
