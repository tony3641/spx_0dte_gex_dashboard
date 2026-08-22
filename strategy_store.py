"""JSON persistence for strategies. Fail-safe (malformed file -> empty)."""
import json
import logging
from pathlib import Path
from typing import Dict, Optional

from strategy_models import Strategy

logger = logging.getLogger(__name__)

STRATEGIES_PATH = Path(__file__).parent / "config" / "strategies.json"


def _resolve(path) -> Path:
    return Path(path) if path is not None else STRATEGIES_PATH


def load_strategies(path=None) -> Dict[str, Strategy]:
    p = _resolve(path)
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {name: Strategy.from_dict(d) for name, d in data.items()}
    except Exception as e:
        logger.error(f"Failed to load strategies from {p}: {e} — starting empty")
        return {}


def save_strategies(path, strategies: Dict[str, Strategy]) -> None:
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {name: s.to_dict() for name, s in strategies.items()}
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(p)   # atomic


def save_strategy(path, strategy: Strategy) -> None:
    strategies = load_strategies(path)
    strategies[strategy.name] = strategy
    save_strategies(path, strategies)


def delete_strategy(path, name: str) -> bool:
    strategies = load_strategies(path)
    if name not in strategies:
        return False
    del strategies[name]
    save_strategies(path, strategies)
    return True
