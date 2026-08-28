"""JSON persistence for watchlists. Fail-safe (malformed file -> empty)."""
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

WATCHLISTS_PATH = Path(__file__).parent / "config" / "watchlists.json"


@dataclass
class WatchlistEntry:
    symbol: str = ""
    sec_type: str = "STK"          # "STK" | "IND"
    exchange: str = "SMART"
    display_name: str = ""

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "sec_type": self.sec_type,
                "exchange": self.exchange, "display_name": self.display_name}

    @classmethod
    def from_dict(cls, d: dict) -> "WatchlistEntry":
        return cls(symbol=str(d.get("symbol", "")),
                   sec_type=d.get("sec_type", "STK"),
                   exchange=str(d.get("exchange", "SMART")),
                   display_name=str(d.get("display_name", "")))


@dataclass
class Watchlist:
    name: str
    entries: List[WatchlistEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "entries": [e.to_dict() for e in self.entries]}

    @classmethod
    def from_dict(cls, d: dict) -> "Watchlist":
        return cls(name=str(d.get("name", "")),
                   entries=[WatchlistEntry.from_dict(e) for e in d.get("entries", [])])


def _clean_entry(entry: WatchlistEntry) -> WatchlistEntry:
    sym = str(entry.symbol or "").strip().upper()
    sec = str(entry.sec_type or "STK").upper()
    if sec not in ("STK", "IND"):
        sec = "STK"
    ex = str(entry.exchange or "SMART").upper() or "SMART"
    if not sym:
        return WatchlistEntry("", "STK", "SMART", "")
    return WatchlistEntry(symbol=sym, sec_type=sec, exchange=ex,
                          display_name=str(entry.display_name or ""))


class WatchlistStore:
    def __init__(self, path: Path = None):
        self.path = Path(path) if path is not None else WATCHLISTS_PATH
        self.watchlists: Dict[str, Watchlist] = {}

    def load(self) -> None:
        self.watchlists = {}
        p = self.path
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            items = data.get("watchlists", []) if isinstance(data, dict) else []
            for w in items:
                wl = Watchlist.from_dict(w)
                if wl.name:
                    self.watchlists[wl.name] = wl
        except Exception as e:
            logger.error(f"Failed to load watchlists from {p}: {e} — starting empty")
            self.watchlists = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"watchlists": [w.to_dict() for w in self.watchlists.values()]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def all(self) -> List[Watchlist]:
        return list(self.watchlists.values())

    def get(self, name: str) -> Optional[Watchlist]:
        return self.watchlists.get(name)

    def create(self, name: str) -> bool:
        if not name or name in self.watchlists:
            return False
        self.watchlists[name] = Watchlist(name=name, entries=[])
        return True

    def delete(self, name: str) -> bool:
        if name not in self.watchlists:
            return False
        del self.watchlists[name]
        return True

    def add_entry(self, name: str, entry: WatchlistEntry) -> bool:
        wl = self.watchlists.get(name)
        if wl is None:
            return False
        e = _clean_entry(entry)
        if not e.symbol:
            return False
        if any(x.symbol == e.symbol for x in wl.entries):
            return False
        wl.entries.append(e)
        return True

    def remove_entry(self, name: str, symbol: str) -> bool:
        wl = self.watchlists.get(name)
        if wl is None:
            return False
        before = len(wl.entries)
        sym = str(symbol or "").upper()
        wl.entries = [x for x in wl.entries if x.symbol != sym]
        return len(wl.entries) < before
