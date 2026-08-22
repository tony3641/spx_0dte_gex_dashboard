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
        data = {name: Strategy.from_dict(d) for name, d in data.items()}
        return validate_strategy_tree(data)
    except Exception as e:
        logger.error(f"Failed to load strategies from {p}: {e} — starting empty")
        return {}


def validate_strategy_tree(strategies) -> dict:
    """Keep only strategies that are valid in the parent/child tree.

    Drops (fail-safe, logged): a child whose parent is missing, a child with no
    enabled trigger, and any strategy in a parent cycle. A master is always kept.
    """
    ok = dict(strategies)

    def own_invalid(s):
        if s.parent_name == "":
            return False
        return not any(t.enabled for t in s.subsequent_triggers)

    changed = True
    while changed:
        changed = False
        names = set(ok)
        for n in list(ok):
            s = ok[n]
            if own_invalid(s):
                logger.error(f"Dropping strategy '{n}': child must have >=1 enabled trigger")
                del ok[n]; changed = True; continue
            if s.parent_name != "" and s.parent_name not in names:
                logger.error(f"Dropping strategy '{n}': parent '{s.parent_name}' not found")
                del ok[n]; changed = True; continue
        names = set(ok)
        for n in list(ok):
            seen = set(); cur = n
            while cur in ok and cur != "":
                if cur in seen:
                    logger.error(f"Dropping strategy '{n}': parent cycle detected")
                    del ok[n]; changed = True; break
                seen.add(cur)
                cur = ok[cur].parent_name
    return ok


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
