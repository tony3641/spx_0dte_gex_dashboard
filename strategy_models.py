"""Pure data models for a credit-spread trading strategy."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


DIRECTIONS = ("bull_put", "bear_call")
TREND_INDICATORS = ("rsi", "pmove")
BUCKET_OPS = ("above", "below", "range")


@dataclass
class Condition:
    kind: str                                    # entry_window|short_delta|spread_width|credit|trend|volatility
    enabled: bool = True
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "enabled": self.enabled, "params": dict(self.params)}

    @classmethod
    def from_dict(cls, d: dict) -> "Condition":
        return cls(kind=d["kind"], enabled=d.get("enabled", True), params=dict(d.get("params") or {}))


@dataclass
class TakeProfit:
    mode: str = "pct_credit"    # pct_credit | dollar | credit_price
    value: float = 0.0

    def to_dict(self) -> dict:
        return {"mode": self.mode, "value": self.value}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["TakeProfit"]:
        if not d:
            return None
        return cls(mode=d.get("mode", "pct_credit"), value=float(d.get("value", 0.0)))


@dataclass
class StopLoss:
    """Stop-loss as a single multiplier of the collected credit.

    The IB stop-limit bracket's trigger price = |credit| * multiplier (signed
    negative, e.g. credit -0.30 with multiplier 5.0 -> -1.50). The engine
    derives the bracket stop/limit from this one input.
    """
    multiplier: float = 1.0

    def to_dict(self) -> dict:
        return {"multiplier": self.multiplier}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["StopLoss"]:
        if not d:
            return None
        return cls(multiplier=float(d.get("multiplier", 1.0)))


@dataclass
class ExitRules:
    take_profit: Optional[TakeProfit] = None
    stop_loss: Optional[StopLoss] = None
    hold_to_expire: bool = False

    def to_dict(self) -> dict:
        return {
            "take_profit": self.take_profit.to_dict() if self.take_profit else None,
            "stop_loss": self.stop_loss.to_dict() if self.stop_loss else None,
            "hold_to_expire": self.hold_to_expire,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExitRules":
        return cls(
            take_profit=TakeProfit.from_dict(d.get("take_profit")),
            stop_loss=StopLoss.from_dict(d.get("stop_loss")),
            hold_to_expire=bool(d.get("hold_to_expire", False)),
        )


@dataclass
class Strategy:
    name: str
    direction: str
    conditions: List[Condition]
    exit_rules: ExitRules = field(default_factory=ExitRules)
    auto_execute: bool = False
    armed: bool = False
    target_expiry: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "direction": self.direction,
            "conditions": [c.to_dict() for c in self.conditions],
            "exit_rules": self.exit_rules.to_dict(),
            "auto_execute": self.auto_execute,
            "armed": self.armed,
            "target_expiry": self.target_expiry,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Strategy":
        direction = d["direction"]
        if direction not in DIRECTIONS:
            raise ValueError(f"Invalid direction: {direction}")
        return cls(
            name=d["name"],
            direction=direction,
            conditions=[Condition.from_dict(c) for c in d.get("conditions", [])],
            exit_rules=ExitRules.from_dict(d.get("exit_rules") or {}),
            auto_execute=bool(d.get("auto_execute", False)),
            armed=bool(d.get("armed", False)),
            target_expiry=d.get("target_expiry", ""),
        )
