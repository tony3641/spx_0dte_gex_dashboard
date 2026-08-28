import json
from pathlib import Path
from watchlist_store import WatchlistStore, WatchlistEntry


def _new(tmp_path):
    return WatchlistStore(path=tmp_path / "watchlists.json")


def test_load_missing_resets_empty(tmp_path):
    store = _new(tmp_path)
    store.load()
    assert store.all() == []


def test_load_malformed_resets_empty(tmp_path):
    p = tmp_path / "watchlists.json"
    p.write_text("{not json}", encoding="utf-8")
    store = _new(tmp_path)
    store.load()
    assert store.all() == []


def test_create_add_entry_and_save_roundtrip(tmp_path):
    store = _new(tmp_path)
    store.load()
    assert store.create("Main") is True
    assert store.add_entry("Main", WatchlistEntry(symbol="SPX", sec_type="IND")) is True
    store.save()

    store2 = _new(tmp_path)
    store2.load()
    wl = store2.get("Main")
    assert wl is not None
    assert wl.entries[0].symbol == "SPX"
    assert wl.entries[0].sec_type == "IND"


def test_add_entry_dedupes_by_symbol(tmp_path):
    store = _new(tmp_path)
    store.load()
    store.create("Main")
    store.add_entry("Main", WatchlistEntry(symbol="SPX", sec_type="IND"))
    assert store.add_entry("Main", WatchlistEntry(symbol="spx", sec_type="IND")) is False
    assert len(store.get("Main").entries) == 1


def test_remove_and_delete(tmp_path):
    store = _new(tmp_path)
    store.load()
    store.create("Main")
    store.add_entry("Main", WatchlistEntry(symbol="SPX", sec_type="IND"))
    assert store.remove_entry("Main", "SPX") is True
    assert store.delete("Main") is True
    assert store.delete("Main") is False


def test_clean_entry_drops_empty_and_coerces_type(tmp_path):
    store = _new(tmp_path)
    store.load()
    store.create("Main")
    assert store.add_entry("Main", WatchlistEntry(symbol="   ", sec_type="FUT")) is False
    assert store.add_entry("Main", WatchlistEntry(symbol="spy", sec_type="future")) is True
    assert store.get("Main").entries[0].sec_type == "STK"
    assert store.get("Main").entries[0].symbol == "SPY"
