import pytest
from strategy_models import Strategy, Condition
from strategy_engine import generate_candidates, evaluate_conditions


def _rows():
    return {"strikes": [
        {"strike": 5100, "put_bid": 1.0, "put_ask": 1.4, "put_delta": -0.70,
         "call_bid": 9.0, "call_ask": 9.6, "call_delta": 0.60, "call_iv": 20.0, "put_iv": 21.0},
        {"strike": 5200, "put_bid": 3.0, "put_ask": 3.4, "put_delta": -0.30,
         "call_bid": 4.0, "call_ask": 4.6, "call_delta": 0.30, "call_iv": 18.0, "put_iv": 19.0},
        {"strike": 5300, "put_bid": 6.0, "put_ask": 6.6, "put_delta": -0.10,
         "call_bid": 2.0, "call_ask": 2.6, "call_delta": 0.10, "call_iv": 22.0, "put_iv": 23.0},
    ], "spot_price": 5200.0, "annual_vol": 0.2}


def test_bear_call_candidates():
    strat = Strategy(name="bc", direction="bear_call", conditions=[
        Condition(kind="short_delta", params={"min": 0.2, "max": 0.4}),
        Condition(kind="spread_width", params={"min": 50, "max": 150}),
    ])
    state = type("S", (), {"chain_quotes_cache": _rows(), "account_summary": {}})()
    cands = generate_candidates(strat, state)
    assert cands
    c = cands[0]
    assert c.direction == "bear_call"
    assert c.short_strike == 5200 and c.long_strike == 5300
    assert c.width_points == 100 and c.margin == 10000
    assert c.credit_mid > 0
    assert c.credit_mid == pytest.approx((4.3 - 2.3), abs=0.01)


def test_unbounded_max_when_min_only():
    # A present condition with only `min` means the max side is unlimited (n/a),
    # not the old hardcoded 0.35 cap: a 0.70-abs-delta short must be allowed.
    rows = {"strikes": [
        {"strike": 5000, "put_bid": 1.0, "put_ask": 1.4, "put_delta": -0.80},
        {"strike": 5100, "put_bid": 3.0, "put_ask": 3.4, "put_delta": -0.70},
        {"strike": 5200, "put_bid": 6.0, "put_ask": 6.6, "put_delta": -0.30},
    ], "spot_price": 5200.0}
    strat = Strategy(name="t", direction="bull_put", conditions=[
        Condition(kind="spread_width", params={"min": 1, "max": 10000}),  # open the width filter
        Condition(kind="short_delta", params={"min": 0.2}),               # no max -> unbounded upper
    ])
    state = type("S", (), {"chain_quotes_cache": rows, "account_summary": {}})()
    strikes = {c.short_strike for c in generate_candidates(strat, state)}
    assert 5100.0 in strikes   # short put delta -0.70


def test_has_margin_budget_caps():
    from strategy_engine import _has_margin
    state = type("S", (), {"account_summary": {"ExcessLiquidity": 100000.0}})()
    cand = type("C", (), {"margin": 5000.0})()
    assert _has_margin(state, cand, budget=10000.0) is True
    assert _has_margin(state, cand, budget=3000.0) is False   # budget is the binding cap


def _closes():
    return [100.0 + 0.2 * i for i in range(20)]  # gently rising


def _state(vix=15.0, spot=5200.0):
    rows = {"strikes": [
        {"strike": 5100, "put_bid": 1.0, "put_ask": 1.4, "put_delta": -0.70, "put_iv": 21.0,
         "call_bid": 9.0, "call_ask": 9.6, "call_delta": 0.30, "call_iv": 20.0},
        {"strike": 5200, "put_bid": 3.0, "put_ask": 3.4, "put_delta": -0.30, "put_iv": 19.0,
         "call_bid": 4.0, "call_ask": 4.6, "call_delta": 0.30, "call_iv": 18.0},
        {"strike": 5300, "put_bid": 6.0, "put_ask": 6.6, "put_delta": -0.10, "put_iv": 23.0,
         "call_bid": 2.0, "call_ask": 2.6, "call_delta": 0.10, "call_iv": 22.0},
    ], "spot_price": spot}
    class S: pass
    s = S()
    s.chain_quotes_cache = rows
    s.spx_price = spot
    s.vix = vix
    s.price_history = [{"close": c} for c in _closes()]
    s.account_summary = {}
    return s


def _swe(d="bull_put"):
    return Strategy(name="t", direction=d, conditions=[
        Condition(kind="entry_window", params={"start": "09:32", "end": "11:00"}),
        Condition(kind="volatility", params={"vix_enabled": True, "vix_op": "above", "vix_value": 14.0,
                                             "atm_iv_enabled": False}),
        Condition(kind="spread_width", params={"min": 50, "max": 150}),
        Condition(kind="short_delta", params={"min": 0.2, "max": 0.4}),
    ])


def test_fail_window_blocker(monkeypatch):
    import datetime as dt
    from strategy_engine import evaluate_conditions
    now = dt.datetime(2026, 8, 21, 12, 30)
    ev = evaluate_conditions(_swe(), _state(), now=now)
    assert ev.status == "blocked"
    assert ev.blocker == "entry_window"


def test_all_pass_ready():
    import datetime as dt
    now = dt.datetime(2026, 8, 21, 10, 0)
    ev = evaluate_conditions(_swe(), _state(vix=20.0), now=now)
    assert ev.status == "ready"
    assert ev.candidates


def test_vix_blocks_before_candidates():
    import datetime as dt
    now = dt.datetime(2026, 8, 21, 10, 0)
    ev = evaluate_conditions(_swe(), _state(vix=10.0), now=now)
    assert ev.blocker == "volatility"
    assert ev.candidates == []


import pytest, asyncio
from strategy_engine import place_strategy_entry, _build_entry_payload


def _state_t8(spot=5200.0):
    rows = {"strikes": [
        {"strike": 5100, "put_bid": 1.0, "put_ask": 1.4, "put_delta": -0.70, "put_iv": 21.0,
         "call_bid": 9.0, "call_ask": 9.6, "call_delta": 0.30, "call_iv": 20.0},
        {"strike": 5200, "put_bid": 3.0, "put_ask": 3.4, "put_delta": -0.30, "put_iv": 19.0,
         "call_bid": 4.0, "call_ask": 4.6, "call_delta": 0.30, "call_iv": 18.0},
        {"strike": 5300, "put_bid": 6.0, "put_ask": 6.6, "put_delta": -0.10, "put_iv": 23.0,
         "call_bid": 2.0, "call_ask": 2.6, "call_delta": 0.10, "call_iv": 22.0},
    ], "spot_price": spot}
    class S: pass
    s = S()
    s.chain_quotes_cache = rows
    s.spx_price = spot
    s.expiration = "20260821"
    s.positions = []
    return s


def test_build_entry_payload_credit_negative():
    strat = Strategy(name="t", direction="bear_call", conditions=[Condition(kind="short_delta", params={"min": 0.2, "max": 0.4})])
    cand = type("C", (), {"direction": "bear_call", "short_strike": 5200.0, "long_strike": 5300.0,
                          "credit_mid": 2.0})()
    p = _build_entry_payload(strat, cand, _state_t8())
    assert p["comboLmtPrice"] < 0
    assert p["legs"][0]["action"] == "SELL"
    assert p["legs"][1]["action"] == "BUY"


def test_build_entry_payload_stop_loss_multiplier():
    import pytest
    from strategy_models import StopLoss, ExitRules
    from strategy_engine import Candidate, _build_entry_payload
    strat = Strategy(
        name="t", direction="bear_call",
        conditions=[Condition(kind="short_delta", params={"min": 0.2, "max": 0.4})],
        exit_rules=ExitRules(stop_loss=StopLoss(multiplier=5.0)),
    )
    cand = Candidate(direction="bear_call", short_strike=5200.0, long_strike=5300.0, width_points=100.0,
                     margin=10000.0, credit_bid=1.8, credit_ask=2.2, credit_mid=0.30,
                     short_delta=0.3, long_delta=0.1, atm_iv=18.0)
    p = _build_entry_payload(strat, cand, _state_t8())
    assert p["stopLoss"] is not None
    assert p["stopLoss"]["stopPrice"] == pytest.approx(-1.5, abs=0.01)
    assert p["stopLoss"]["limitPrice"] == pytest.approx(-1.5, abs=0.01)


def test_build_entry_payload_includes_trading_class():
    """Legs must carry the strategy's trading_class so monthly SPX fallback
    resolves correctly; default is SPXW."""
    from strategy_engine import _build_entry_payload
    strat = Strategy(name="t", direction="bear_call",
                     conditions=[Condition(kind="short_delta", params={"min": 0.2, "max": 0.4})])
    cand = type("C", (), {"direction": "bear_call", "short_strike": 5200.0, "long_strike": 5300.0,
                          "credit_mid": 2.0})()
    # No trading_class attr -> default SPXW
    p = _build_entry_payload(strat, cand, _state_t8())
    assert p["legs"][0]["trading_class"] == "SPXW"
    assert p["legs"][1]["trading_class"] == "SPXW"
    # state.trading_class -> threads through
    st = _state_t8()
    st.trading_class = "SPX"
    p2 = _build_entry_payload(strat, cand, st)
    assert p2["legs"][0]["trading_class"] == "SPX"
    assert p2["legs"][1]["trading_class"] == "SPX"


def test_option_contract_trading_class_default_and_override():
    """order_manager._option_contract defaults to SPXW but honors an override."""
    from order_manager import _option_contract
    assert _option_contract("SPX", "20260821", 5200.0, "C", "CBOE").tradingClass == "SPXW"
    assert _option_contract("SPX", "20260821", 5200.0, "C", "CBOE", trading_class="SPX").tradingClass == "SPX"


@pytest.mark.asyncio
async def test_place_strategy_entry_via_mock(mock_ib, app_state):
    from strategy_engine import _build_entry_payload, place_strategy_entry, Candidate
    app_state.expiration = "20260821"
    strat = Strategy(name="t", direction="bear_call", conditions=[Condition(kind="short_delta", params={"min": 0.2, "max": 0.4})])
    cand = Candidate(direction="bear_call", short_strike=5200.0, long_strike=5300.0, width_points=100.0,
                     margin=10000.0, credit_bid=1.8, credit_ask=2.2, credit_mid=2.0,
                     short_delta=0.3, long_delta=0.1, atm_iv=18.0)
    payload = _build_entry_payload(strat, cand, app_state)
    resp = await place_strategy_entry(mock_ib, app_state, strat, cand)
    assert resp is not None
    assert len(app_state.strategy_log) == 1
    assert len(mock_ib.get_placed_orders()) == 1


def test_has_open_position():
    from strategy_engine import _has_open_position
    state = type("S", (), {"positions": [{"contract": {"strike": 5200.0, "right": "C"}}]})()
    sig = {(5200.0, "C"), (5300.0, "C")}
    assert _has_open_position(state, sig) is True
    assert _has_open_position(type("S", (), {"positions": []})(), sig) is False
    other = type("S", (), {"positions": [{"contract": {"strike": 5000.0, "right": "P"}}]})()
    assert _has_open_position(other, sig) is False


@pytest.mark.asyncio
async def test_eval_loop_skips_strategy_with_open_position(monkeypatch, mock_ib, app_state):
    """spec §11: one concurrent position per strategy.

    An armed + auto_execute strategy whose name is already in
    state.strategy_open_positions must NOT place an order through the eval
    loop, while an open strategy still does (positive control).
    """
    import asyncio
    from market_hours import now_et
    from strategy_engine import strategy_evaluation_loop, StrategyEval, Candidate

    app_state.connected = True
    app_state.account_summary = {"ExcessLiquidity": 100000.0}   # margin check passes
    app_state.expirations = [now_et().date().strftime("%Y%m%d")]  # today is tradeable (holiday gate passes)

    def _strat(name):
        # Make the schedule gate deterministic regardless of the real run date.
        return Strategy(name=name, direction="bear_call", auto_execute=True, armed=True,
                        conditions=[Condition(kind="short_delta", params={"min": 0.2, "max": 0.4})],
                        run_days=[now_et().weekday()], short_day_enabled=True,
                        run_on_fomc=True, run_on_nfp=True)

    s_open = _strat("open_strat")
    s_guard = _strat("guard_strat")
    app_state.strategies = {"open_strat": s_open, "guard_strat": s_guard}

    cand = Candidate(direction="bear_call", short_strike=5200.0, long_strike=5300.0, width_points=100.0,
                     margin=10000.0, credit_bid=1.8, credit_ask=2.2, credit_mid=2.0,
                     short_delta=0.3, long_delta=0.1, atm_iv=18.0)
    # Seed an already-open position for guard_strat.
    app_state.strategy_open_positions = {"guard_strat": cand.to_dict()}

    placed = []
    async def fake_place_entry(ib, state, strategy, candidate):
        placed.append(strategy.name)
        return {"type": "order_status", "data": {"status": "Filled"}}
    def fake_eval(strategy, state, now=None, candidates=None):
        return StrategyEval(status="ready", candidates=[cand])
    async def fake_broadcast(message):
        pass

    monkeypatch.setattr("strategy_engine.place_strategy_entry", fake_place_entry)
    monkeypatch.setattr("strategy_engine.evaluate_conditions", fake_eval)

    task = asyncio.create_task(strategy_evaluation_loop(mock_ib, app_state, fake_broadcast))
    await asyncio.sleep(3.5)   # one full 3s cadence
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Only the unguarded strategy placed; the seeded one was skipped.
    assert placed == ["open_strat"]
    assert "guard_strat" not in placed


from strategy_engine import signature_for_candidate, find_strategy_positions
from strategy_models import Strategy, Condition


def test_match_positions():
    state = type("S", (), {
        "positions": [
            {"contract": {"strike": 5200.0, "right": "C"}, "unrealizedPNL": 100.0},
            {"contract": {"strike": 5300.0, "right": "C"}, "unrealizedPNL": 100.0},
            {"contract": {"strike": 5000.0, "right": "P"}, "unrealizedPNL": 50.0},
        ],
        "expiration": "20260821",
    })()
    cand = type("C", (), {"direction": "bear_call", "short_strike": 5200.0, "long_strike": 5300.0})()
    sig = signature_for_candidate(cand, state)
    found = find_strategy_positions(cand, state)
    # both legs present -> matched
    assert len(found) == 2


import asyncio, pytest
from strategy_engine import maybe_flatten_at_take_profit, Candidate
from strategy_models import TakeProfit

# ---------------------------------------------------------------------------
# Schedule gates: _strategy_should_run_today (day-of-week / holiday / half-day / FOMC / NFP)
# ---------------------------------------------------------------------------

def _gate_state(exps=(), monthly=()):
    class S:
        pass
    s = S()
    s.expirations = list(exps)
    s.monthly_expirations = list(monthly)
    return s


def test_gate_skips_non_run_day():
    from datetime import date
    from strategy_engine import _strategy_should_run_today
    s = Strategy(name="t", direction="bull_put", conditions=[], run_days=[0, 1, 2])  # Mon-Wed
    st = _gate_state(["20260821"])   # Friday 0DTE present
    assert _strategy_should_run_today(s, st, today=date(2026, 8, 21)) is False


def test_gate_runs_on_run_day():
    from datetime import date
    from strategy_engine import _strategy_should_run_today
    s = Strategy(name="t", direction="bull_put", conditions=[], run_days=[4])
    st = _gate_state(["20260821"])
    assert _strategy_should_run_today(s, st, today=date(2026, 8, 21)) is True


def test_gate_skips_holiday_no_0dte():
    """Today has no expiring option -> holiday, no trade."""
    from datetime import date
    from strategy_engine import _strategy_should_run_today
    s = Strategy(name="t", direction="bull_put", conditions=[], run_days=[4])
    st = _gate_state(["20260828"])   # today (8/21) not in list
    assert _strategy_should_run_today(s, st, today=date(2026, 8, 21)) is False


def test_gate_skips_half_day_when_disabled():
    from datetime import date
    from strategy_engine import _strategy_should_run_today
    s = Strategy(name="t", direction="bull_put", conditions=[], run_days=[3], short_day_enabled=False)
    st = _gate_state(["20261224"])   # 2026-12-24 is an early-close (half) session
    assert _strategy_should_run_today(s, st, today=date(2026, 12, 24)) is False


def test_gate_runs_half_day_when_enabled():
    from datetime import date
    from strategy_engine import _strategy_should_run_today
    s = Strategy(name="t", direction="bull_put", conditions=[], run_days=[3], short_day_enabled=True)
    st = _gate_state(["20261224"])
    assert _strategy_should_run_today(s, st, today=date(2026, 12, 24)) is True


def test_gate_skips_fomc_when_disabled():
    from datetime import date
    from strategy_engine import _strategy_should_run_today
    s = Strategy(name="t", direction="bull_put", conditions=[], run_days=[2], run_on_fomc=False)
    st = _gate_state(["20260128"])   # 2026-01-28 is a scheduled FOMC meeting day
    assert _strategy_should_run_today(s, st, today=date(2026, 1, 28)) is False


def test_gate_skips_nfp_when_disabled():
    from datetime import date
    from strategy_engine import _strategy_should_run_today
    s = Strategy(name="t", direction="bull_put", conditions=[], run_days=[4], run_on_nfp=False)
    st = _gate_state(["20260102"])   # 2026-01-02 is the first Friday (NFP)
    assert _strategy_should_run_today(s, st, today=date(2026, 1, 2)) is False


def test_gate_permissive_when_expiration_data_unloaded():
    """No expiration data yet is not a holiday — the engine should not over-skip."""
    from datetime import date
    from strategy_engine import _strategy_should_run_today
    s = Strategy(name="t", direction="bull_put", conditions=[], run_days=[4])
    st = _gate_state([], [])
    assert _strategy_should_run_today(s, st, today=date(2026, 8, 21)) is True


@pytest.mark.asyncio
async def test_maybe_flatten_below_target_does_not_place(mock_ib, app_state):
    app_state.chain_quotes_cache = {"strikes": []}   # no quotes -> cannot price a close
    app_state.expiration = "20260821"
    cand = Candidate(direction="bear_call", short_strike=5200.0, long_strike=5300.0, width_points=100.0,
                     margin=10000.0, credit_bid=1.8, credit_ask=2.2, credit_mid=2.0,
                     short_delta=0.3, long_delta=0.1, atm_iv=18.0)
    pos = [{"contract": {"strike": 5200.0, "right": "C"}, "unrealizedPNL": 0.5},
           {"contract": {"strike": 5300.0, "right": "C"}, "unrealizedPNL": 0.2}]
    tp = TakeProfit(mode="pct_credit", value=0.5)   # target 1.0; net 0.7 below
    closed = await maybe_flatten_at_take_profit(mock_ib, app_state, cand, pos, tp)
    assert closed is False
    assert len(mock_ib.get_placed_orders()) == 0


import datetime as dt
import time
from strategy_models import TriggerSpec, RuntimeState
from strategy_engine import get_runtime, reset_strategy_runtime, _child_is_eligible


def _parent_rt(entered=True, done=True, credit=0.30, high=1.5, low=-2.5, close_reason="stop_loss"):
    rt = RuntimeState(entered=entered, done=done)
    # Full candidate dict so _child_is_eligible can rebuild Candidate and check
    # find_strategy_positions against it (spec §3 flat-parent gate).
    rt.trade = {"candidate": {"direction": "bull_put", "short_strike": 5100.0, "long_strike": 5000.0,
                              "width_points": 100.0, "margin": 10000.0, "credit_bid": 0.2, "credit_ask": 0.4,
                              "credit_mid": 0.30, "short_delta": 0.3, "long_delta": 0.1, "atm_iv": 18.0},
                "credit": credit, "high_water_mult": high, "low_water_mult": low,
                "close_reason": close_reason}
    return rt


def _populate():
    state = type("S", (), {})()
    state.strategies = {
        "master": Strategy(name="master", direction="bull_put", conditions=[]),
        "recovery": Strategy(
            name="recovery", direction="bull_put", conditions=[],
            parent_name="master",
            subsequent_triggers=[TriggerSpec(kind="parent_exit_reason", params={"reason": "stop_loss"})],
            trigger_logic="any",
        ),
    }
    state.runtime = {"master": _parent_rt(), "recovery": RuntimeState()}
    state.strategy_open_positions = {}   # mirrors AppState (engine pops entries on reset)
    return state


def test_get_runtime_creates_on_demand():
    st = _populate()
    rt = get_runtime(st, "master")
    assert rt is st.runtime["master"]
    assert get_runtime(st, "new").cycle == 0


def test_trigger_exit_reason_fires_when_parent_closed():
    from strategy_engine import _trigger_aggregate
    st = _populate()
    assert _trigger_aggregate(st.strategies["recovery"], st) is True


def test_child_not_eligible_while_parent_open():
    st = _populate()
    st.runtime["master"].done = False           # parent still open
    assert _child_is_eligible(st.strategies["recovery"], st) is False


def test_child_eligible_when_parent_done_and_trigger_fired():
    st = _populate()
    assert _child_is_eligible(st.strategies["recovery"], st) is True


def test_child_not_eligible_after_it_entered():
    st = _populate()
    st.runtime["recovery"].entered = True
    assert _child_is_eligible(st.strategies["recovery"], st) is False


def test_child_not_eligible_while_parent_close_legs_present():
    """A parent marked done (TP close accepted) whose legs are still present in
    state.positions is NOT flat yet — the child must wait (spec §3)."""
    st = _populate()
    st.positions = [{"contract": {"strike": 5100.0, "right": "P"}, "unrealizedPNL": 0.0},
                    {"contract": {"strike": 5000.0, "right": "P"}, "unrealizedPNL": 0.0}]
    assert _child_is_eligible(st.strategies["recovery"], st) is False   # close legs still working
    st.positions = []   # parent now truly flat
    assert _child_is_eligible(st.strategies["recovery"], st) is True


def test_trigger_pnl_gain_multiple():
    from strategy_engine import _trigger_aggregate
    st = _populate()
    st.strategies["recovery"].subsequent_triggers = [
        TriggerSpec(kind="parent_unrealized_pnl", params={"gain_multiple": 1.0, "loss_multiple": 2.0})]
    assert _trigger_aggregate(st.strategies["recovery"], st) is True    # high 1.5 >= 1.0


def test_trigger_time_of_day_window():
    from strategy_engine import _trigger_aggregate
    st = _populate()
    st.strategies["recovery"].subsequent_triggers = [
        TriggerSpec(kind="time_of_day", params={"start": "13:00", "end": "15:00"})]
    now = dt.datetime(2026, 8, 21, 14, 0)
    assert _trigger_aggregate(st.strategies["recovery"], st, now=now) is True
    assert _trigger_aggregate(st.strategies["recovery"], st, now=dt.datetime(2026, 8, 21, 12, 0)) is False


def test_reset_strategy_runtime_increments_cycle():
    st = _populate()
    reset_strategy_runtime(st, "master")
    assert st.runtime["master"].cycle == 1
    assert st.runtime["master"].entered is False
    assert st.runtime["master"].trade is None


def test_reset_strategy_runtime_clears_stale_open_position():
    """A stale strategy_open_positions entry (non-TP close) must not re-block
    a re-armed strategy; re-arm pops it (spec §6)."""
    st = _populate()
    st.strategy_open_positions = {"master": {"whatever": 1}}
    reset_strategy_runtime(st, "master")
    assert "master" not in st.strategy_open_positions


import asyncio
from strategy_engine import (classify_parent_close, _refresh_trade_credit,
                             _update_parent_role, _daily_reset, fire_children)
from strategy_models import ExitRules, StopLoss


def _pos_state(positions=None, executions=None):
    class S:
        pass
    s = S()
    s.positions = positions or []
    s.executions = executions or []
    s.expiration = "20260821"
    return s


def _cand():
    return Candidate(direction="bull_put", short_strike=5100.0, long_strike=5000.0, width_points=100.0,
                     margin=10000.0, credit_bid=0.2, credit_ask=0.4, credit_mid=0.30,
                     short_delta=0.3, long_delta=0.1, atm_iv=18.0)


def test_classify_stop_loss_when_configured():
    strat = Strategy(name="p", direction="bull_put", conditions=[],
                     exit_rules=ExitRules(stop_loss=StopLoss(multiplier=5.0)))
    assert classify_parent_close(strat, _cand(), _pos_state()) == "stop_loss"


def test_classify_manual_when_no_stop_and_future_expiry():
    from datetime import timedelta
    from market_hours import now_et
    strat = Strategy(name="p", direction="bull_put", conditions=[])
    st = _pos_state()
    st.expiration = (now_et().date() + timedelta(days=1)).strftime("%Y%m%d")  # future -> not expired
    assert classify_parent_close(strat, _cand(), st) == "manual"


def test_classify_expire_when_past_expiry():
    from datetime import timedelta
    from market_hours import now_et
    strat = Strategy(name="p", direction="bull_put", conditions=[])
    st = _pos_state()
    st.expiration = (now_et().date() - timedelta(days=1)).strftime("%Y%m%d")  # past -> expired
    assert classify_parent_close(strat, _cand(), st) == "expire"


def test_refresh_credit_from_executions():
    trade = {"candidate": {"direction": "bull_put", "short_strike": 5100.0, "long_strike": 5000.0,
                           "credit_mid": 0.30}, "credit": 0.30}
    execs = [
        {"strike": 5100.0, "right": "P", "side": "SLD", "price": 0.35},
        {"strike": 5000.0, "right": "P", "side": "BOT", "price": 0.05},
    ]
    _refresh_trade_credit(_pos_state(executions=execs), trade)
    assert trade["credit"] == pytest.approx(0.30, abs=0.001)


@pytest.mark.asyncio
async def test_update_parent_role_marks_done_and_fires_children():
    from strategy_models import ExitRules, StopLoss
    state = type("S", (), {})()
    state.strategies = {
        "master": Strategy(name="master", direction="bull_put", conditions=[],
                           exit_rules=ExitRules(stop_loss=StopLoss(multiplier=5.0))),
        "child": Strategy(name="child", direction="bull_put", conditions=[],
                          parent_name="master",
                          subsequent_triggers=[TriggerSpec(kind="parent_exit_reason", params={"reason": "stop_loss"})]),
    }
    state.positions = []   # no matching positions -> parent is closed
    state.executions = []
    state.expiration = "20260821"
    state.runtime = {"master": RuntimeState(entered=True)}
    state.runtime["master"].trade = {"candidate": _cand().to_dict(), "credit": 0.30,
                                     "high_water_mult": 0.0, "low_water_mult": 0.0}
    state.runtime["child"] = RuntimeState()
    fired = []
    async def bcast(m):
        fired.append(m)
    await _update_parent_role("master", state, bcast)
    assert state.runtime["master"].done is True
    assert state.runtime["master"].trade["close_reason"] == "stop_loss"
    assert state.runtime["child"].time_met is False   # no time trigger
    assert any(m.get("type") == "strategy_trigger" and m["data"]["name"] == "child"
               for m in fired)


@pytest.mark.asyncio
async def test_update_parent_role_updates_water_marks_while_open():
    state = type("S", (), {})()
    state.strategies = {"master": Strategy(name="master", direction="bull_put", conditions=[]),
                        "child": Strategy(name="child", direction="bull_put", conditions=[],
                                          parent_name="master")}
    state.positions = [{"contract": {"strike": 5100.0, "right": "P"}, "unrealizedPNL": 0.45},
                       {"contract": {"strike": 5000.0, "right": "P"}, "unrealizedPNL": 0.15}]
    state.executions = []
    state.expiration = "20260821"
    cd = _cand().to_dict()
    state.runtime = {"master": RuntimeState(entered=True)}
    state.runtime["master"].trade = {"candidate": cd, "credit": 0.30,
                                     "high_water_mult": 0.0, "low_water_mult": 0.0}
    state.runtime["child"] = RuntimeState()
    await _update_parent_role("master", state)   # still open -> no close
    assert state.runtime["master"].done is False
    mult = (0.45 + 0.15) / 0.30
    assert state.runtime["master"].trade["high_water_mult"] == pytest.approx(mult)


def test_daily_reset_preserves_open_position():
    state = type("S", (), {})()
    state.strategies = {"master": Strategy(name="master", direction="bull_put", conditions=[])}
    state.positions = [{"contract": {"strike": 5100.0, "right": "P"}, "unrealizedPNL": 0.0},
                       {"contract": {"strike": 5000.0, "right": "P"}, "unrealizedPNL": 0.0}]
    state.executions = []
    state.expiration = "20260821"
    cd = _cand().to_dict()
    state.runtime = {"master": RuntimeState(entered=True)}
    state.runtime["master"].trade = {"candidate": cd, "credit": 0.30,
                                     "high_water_mult": 0.0, "low_water_mult": 0.0}
    state.strategy_open_positions = {"master": cd}   # open -> entry stays in the map
    _daily_reset(state)
    assert state.runtime["master"].entered is True       # open -> preserved
    assert state.runtime["master"].trade is not None
    assert "master" in state.strategy_open_positions     # open position still tracked


def test_daily_reset_resets_closed() :
    state = type("S", (), {})()
    state.strategies = {"master": Strategy(name="master", direction="bull_put", conditions=[])}
    state.positions = []   # closed
    state.executions = []
    state.expiration = "20260821"
    cd = _cand().to_dict()
    state.runtime = {"master": RuntimeState(entered=True, done=True)}
    state.runtime["master"].trade = {"candidate": cd, "credit": 0.30,
                                     "high_water_mult": 0.0, "low_water_mult": 0.0}
    state.strategy_open_positions = {"master": cd}   # closed -> stale entry must be popped
    _daily_reset(state)
    assert state.runtime["master"].entered is False
    assert state.runtime["master"].trade is None
    assert "master" not in state.strategy_open_positions


def test_daily_reset_clears_stale_open_position_for_closed():
    """A closed strategy (position gone) with a stale strategy_open_positions
    entry gets it popped by the daily reset (spec §6 lifecycle)."""
    state = type("S", (), {})()
    state.strategies = {"master": Strategy(name="master", direction="bull_put", conditions=[])}
    state.positions = []   # closed
    state.executions = []
    state.expiration = "20260821"
    cd = _cand().to_dict()
    state.runtime = {"master": RuntimeState(entered=True, done=True)}
    state.runtime["master"].trade = {"candidate": cd, "credit": 0.30,
                                     "high_water_mult": 0.0, "low_water_mult": 0.0}
    state.strategy_open_positions = {"master": cd}   # stale map entry
    _daily_reset(state)
    assert state.runtime["master"].entered is False
    assert state.runtime["master"].trade is None
    assert "master" not in state.strategy_open_positions


@pytest.mark.asyncio
async def test_eval_loop_child_enters_once_eligible(monkeypatch):
    import asyncio
    from datetime import date
    from strategy_engine import strategy_evaluation_loop, StrategyEval, Candidate
    from strategy_models import TriggerSpec, RuntimeState

    state = type("S", (), {})()
    state.connected = True
    state.account_summary = {"ExcessLiquidity": 100000.0}
    state.expirations = [date.today().strftime("%Y%m%d")]
    state.strategy_open_positions = {}
    state.strategy_candidates = {}   # the loop writes candidates here each cadence
    state.strategy_log = []
    state.auto_trade_kill_switch = False
    state.runtime = {}
    state.vix = 20.0
    state.spx_price = 5200.0
    state.chain_quotes_cache = {"strikes": []}

    parent = Strategy(name="master", direction="bull_put", conditions=[], armed=True, auto_execute=True,
                      run_days=[date.today().weekday()], short_day_enabled=True, run_on_fomc=True, run_on_nfp=True)
    child = Strategy(name="child", direction="bull_put", conditions=[], armed=True, auto_execute=True,
                     parent_name="master",
                     subsequent_triggers=[TriggerSpec(kind="parent_exit_reason", params={"reason": "stop_loss"})],
                     run_days=[date.today().weekday()], short_day_enabled=True, run_on_fomc=True, run_on_nfp=True)
    state.strategies = {"master": parent, "child": child}

    # Parent has traded and CLOSED (flat) this cycle; the stop-loss trigger fired.
    # Full candidate dict so the flat-parent gate can rebuild Candidate.
    state.runtime["master"] = RuntimeState(entered=True, done=True)
    state.runtime["master"].trade = {"candidate": {"direction": "bull_put", "short_strike": 5100.0,
                                                   "long_strike": 5000.0, "width_points": 100.0,
                                                   "margin": 10000.0, "credit_bid": 0.2, "credit_ask": 0.4,
                                                   "credit_mid": 0.30, "short_delta": 0.3, "long_delta": 0.1,
                                                   "atm_iv": 18.0},
                                     "credit": 0.30, "high_water_mult": 0.0, "low_water_mult": 0.0,
                                     "close_reason": "stop_loss"}
    state.runtime["child"] = RuntimeState()

    placed = []
    cand = Candidate(direction="bull_put", short_strike=5100.0, long_strike=5000.0, width_points=100.0,
                     margin=10000.0, credit_bid=0.2, credit_ask=0.4, credit_mid=0.30,
                     short_delta=0.3, long_delta=0.1, atm_iv=18.0)
    async def fake_place(ib, s, strat, candidate):
        placed.append(strat.name)
        return {"type": "order_status", "data": {"status": "Filled"}}
    def fake_eval(strategy, s, now=None, candidates=None):
        return StrategyEval(status="ready", candidates=[cand])
    async def fake_bcast(message):
        pass
    monkeypatch.setattr("strategy_engine.place_strategy_entry", fake_place)
    monkeypatch.setattr("strategy_engine.evaluate_conditions", fake_eval)

    # The master must NOT enter (already done/entered); the child must NOT be
    # double-entered after entry (one-shot). Each arm/child gate enforced in-loop.
    # We run two cadences: first builds candidates, only the child places.
    # To prove one-shot, we manually mark the child entered after the first run
    # is prevented by the loop guard itself — assert only one placement.

    # First, suppress _strategy_should_run_today so gate is a no-op:
    monkeypatch.setattr("strategy_engine._strategy_should_run_today", lambda strat, s, today=None: True)
    monkeypatch.setattr("strategy_engine._daily_reset", lambda s: None)

    task = asyncio.create_task(strategy_evaluation_loop(None, state, fake_bcast))
    await asyncio.sleep(3.5)
    await asyncio.sleep(3.5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert placed == ["child"]   # only the eligible child placed; parent skipped; child once
