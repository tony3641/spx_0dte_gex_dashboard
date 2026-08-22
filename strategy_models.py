"""Pure data models for a credit-spread trading strategy."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


DIRECTIONS = ("bull_put", "bear_call")
TREND_INDICATORS = ("rsi", "pmove")
BUCKET_OPS = ("above", "below", "range")

DEFAULT_RUN_DAYS = [0, 1, 2, 3, 4]   # Mon=0 .. Fri=4 (all weekdays)


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


TRIGGER_KINDS = ("time_of_day", "parent_exit_reason", "parent_unrealized_pnl")
TRIGGER_LOGIC = ("any", "all")


@dataclass
class TriggerSpec:
    kind: str
    enabled: bool = True
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "enabled": self.enabled, "params": dict(self.params)}

    @classmethod
    def from_dict(cls, d: dict) -> "TriggerSpec":
        return cls(kind=d["kind"], enabled=d.get("enabled", True), params=dict(d.get("params") or {}))


@dataclass
class RuntimeState:
    cycle: int = 0                 # current arming cycle (increments on re-arm / day roll)
    entered: bool = False          # opened the cycle's one position
    done: bool = False             # that position closed; idle until a reset
    trade: Optional[dict] = None   # the single trade record (see strategy_engine)
    time_met: bool = False         # T1: the time window was reached during the parent's current trade
    parent_cycle: int = 0          # the parent cycle time_met was latched against


@dataclass
class Strategy:
    name: str
    direction: str
    conditions: List[Condition]
    exit_rules: ExitRules = field(default_factory=ExitRules)
    auto_execute: bool = False
    armed: bool = False
    target_expiry: str = ""
    budget: Optional[float] = None    # max per-trade margin this strategy may risk (None = no cap)
    run_days: List[int] = field(default_factory=lambda: list(DEFAULT_RUN_DAYS))  # Mon=0..Fri=4
    short_day_enabled: bool = False   # if True, allowed to execute on early-close (half) days
    run_on_fomc: bool = True          # if True, allowed to execute on FOMC days
    run_on_nfp: bool = True           # if True, allowed to execute on NFP (jobs) days
    parent_name: str = ""                                # "" = master (standalone)
    subsequent_triggers: List[TriggerSpec] = field(default_factory=list)
    trigger_logic: str = "any"                           # "any" | "all"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "direction": self.direction,
            "conditions": [c.to_dict() for c in self.conditions],
            "exit_rules": self.exit_rules.to_dict(),
            "auto_execute": self.auto_execute,
            "armed": self.armed,
            "target_expiry": self.target_expiry,
            "budget": self.budget,
            "run_days": list(self.run_days),
            "short_day_enabled": self.short_day_enabled,
            "run_on_fomc": self.run_on_fomc,
            "run_on_nfp": self.run_on_nfp,
            "parent_name": self.parent_name,
            "subsequent_triggers": [t.to_dict() for t in self.subsequent_triggers],
            "trigger_logic": self.trigger_logic,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Strategy":
        direction = d["direction"]
        if direction not in DIRECTIONS:
            raise ValueError(f"Invalid direction: {direction}")
        budget = d.get("budget")
        if budget is not None:
            budget = float(budget)
            if budget < 0:
                raise ValueError(f"Invalid budget: {budget} (must be >= 0)")
        run_days = d.get("run_days")
        if run_days is None:
            run_days = list(DEFAULT_RUN_DAYS)
        else:
            run_days = [int(x) for x in run_days]
            if not run_days or any(x < 0 or x > 4 for x in run_days):
                raise ValueError(f"Invalid run_days: {run_days} (each day must be 0-4)")
        trigger_logic = d.get("trigger_logic", "any")
        if trigger_logic not in TRIGGER_LOGIC:
            raise ValueError(f"Invalid trigger_logic: {trigger_logic}")
        triggers = [TriggerSpec.from_dict(t) for t in d.get("subsequent_triggers", [])]
        for t in triggers:
            if t.kind not in TRIGGER_KINDS:
                raise ValueError(f"Invalid trigger kind: {t.kind}")
        return cls(
            name=d["name"],
            direction=direction,
            conditions=[Condition.from_dict(c) for c in d.get("conditions", [])],
            exit_rules=ExitRules.from_dict(d.get("exit_rules") or {}),
            auto_execute=bool(d.get("auto_execute", False)),
            armed=bool(d.get("armed", False)),
            target_expiry=d.get("target_expiry", ""),
            budget=budget,
            run_days=run_days,
            short_day_enabled=bool(d.get("short_day_enabled", False)),
            run_on_fomc=bool(d.get("run_on_fomc", True)),
            run_on_nfp=bool(d.get("run_on_nfp", True)),
            parent_name=d.get("parent_name", ""),
            subsequent_triggers=triggers,
            trigger_logic=trigger_logic,
        )
