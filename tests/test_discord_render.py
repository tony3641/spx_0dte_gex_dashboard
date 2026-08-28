"""Pure formatting tests for the Discord message renderers.

These exercise the string/color layer only (the part that turns a ``*_view``
dict into readable Discord text). No live Discord gateway is needed and no
``discord.Embed`` is required — the functions under test return plain strings
and color ints.

The bind: each formatter must render the *view* dict as a readable message,
not dump the raw structure. These tests assert the human-facing output.
"""

from discord_bot import (
    _table,
    _money,
    _pct,
    _badge,
    _fmt_candidates,
    _fmt_strategy,
    _fmt_status,
    _fmt_account,
    _fmt_orders,
    _fmt_positions,
    _fmt_action,
    _fmt_alert,
    _run_days_line,
    _position_key,
    _liquidation_payload,
    _liquidatable,
    _embed_color,
)


# ---------------------------------------------------------------------------
# Fixtures — hand-built view dicts (never reuse the renderer's own logic)
# ---------------------------------------------------------------------------
def _cand_rows():
    return [
        {"index": 0, "direction": "bull_put", "short_strike": 7650.0,
         "long_strike": 7600.0, "right": "P", "credit_mid": 2.5,
         "short_delta": -0.10, "width_points": 50.0, "size": 4,
         "total_credit": 10.0, "total_margin": 20000.0},
        {"index": 1, "direction": "bull_put", "short_strike": 7650.0,
         "long_strike": 7605.0, "right": "P", "credit_mid": 1.7,
         "short_delta": -0.20, "width_points": 45.0, "size": 2,
         "total_credit": 3.4, "total_margin": 8000.0},
    ]


def _cand_result():
    return {"ok": True, "name": "New 3", "armed": True, "auto_execute": True,
            "count": 2, "rows": _cand_rows()}


def _empty_cand_result():
    return {"ok": True, "name": "New 3", "armed": False, "auto_execute": False,
            "count": 0, "rows": []}


def _strategy_result():
    from strategy_models import Strategy, Condition
    s = Strategy(
        name="Main", direction="bull_put", budget=15000.0,
        conditions=[
            Condition(kind="short_delta", params={"min": 0.1, "max": 0.4}),
            Condition(kind="spread_width", params={"min": 50, "max": 200}),
        ],
    )
    return {"ok": True, "name": "Main", "strategy": s.to_dict()}


def _status_result():
    return {"connected": True, "market_status": "RTH",
            "expiration": "2026-08-28", "data_mode": "live", "gex_mode": "0dte",
            "spot": 6100.5, "vix": 14.2}


def _account_result():
    return {"summary": {"NetLiquidation": 100000.0, "BuyingPower": 50000.0,
                        "TotalCashValue": 40000.0, "ExcessLiquidity": 25000.0,
                        "UnrealizedPnL": -500.0, "RealizedPnL": 1200.0},
            "positions": [{"contract": {"symbol": "SPX"}, "position": -1}],
            "executions": []}


def _orders_result():
    return {"count": 2, "orders": [
        {"orderId": 1, "status": "Submitted", "action": "BUY", "totalQuantity": 3},
        {"orderId": 2, "status": "Filled", "action": "SELL", "totalQuantity": 1},
    ]}


def _positions_result():
    return {"count": 2, "positions": [
        {"contract": {"symbol": "SPX", "localSymbol": "SPXW", "secType": "OPT",
                      "expiry": "20260626", "strike": 7700.0, "right": "C"},
         "position": -4, "averageCost": 1.85, "unrealizedPNL": -40.0},
        {"contract": {"symbol": "QCOM", "localSymbol": "QCOM", "secType": "STK",
                      "expiry": "", "strike": None, "right": ""},
         "position": -100, "averageCost": 175.2, "unrealizedPNL": 320.0},
    ]}


# ---------------------------------------------------------------------------
# _table
# ---------------------------------------------------------------------------
def test_table_emits_header_separator_and_data():
    out = _table(["name", "score"], [["sam", 95], ["jo", 7]])
    assert "name" in out
    assert "score" in out
    # numeric column is right-aligned so the tens digit lines up
    assert "95" in out and "7" in out
    # a header/data separator row of dashes exists
    assert "---" in out


def test_table_empty_rows_still_show_header():
    out = _table(["a", "b"], [])
    assert "a" in out and "b" in out


# ---------------------------------------------------------------------------
# Number/badge helpers
# ---------------------------------------------------------------------------
def test_money_formats_to_two_places():
    assert _money(2.5) == "2.50"
    assert _money(0) == "0.00"
    assert _money(-1.005) == "-1.00"   # 2 dp, normal rounding
    assert _money(None) == ""


def test_pct_formats_negative_ratio():
    assert _pct(-0.10) == "-10.0%"
    assert _pct(0.5) == "50.0%"
    assert _pct(None) == ""


def test_badge_is_emoji_plus_word():
    assert _badge(True) == "🟢 ON"
    assert _badge(False) == "⚫ OFF"


# ---------------------------------------------------------------------------
# _fmt_candidates
# ---------------------------------------------------------------------------
def test_candidates_is_a_table_not_a_dict_dump():
    out = _fmt_candidates(_cand_result())
    assert "```" in out                       # code block
    assert "**New 3**" in out                 # bold strategy name
    assert "id" in out and "SELL" in out and "BUY" in out
    assert "7,650" in out and "7,600" in out
    assert "2.50" in out                       # credit_mid
    assert "-10.0%" in out                     # short_delta as pct
    assert "20,000" in out                     # total_margin (thousands-separated)


def test_candidates_empty_returns_clean_message():
    out = _fmt_candidates(_empty_cand_result())
    assert "No live candidates" in out
    assert "New 3" in out
    assert "ok" not in "ok: True" or "ok: True" not in out   # not a dict dump


def test_candidates_uses_strategy_metadata():
    out = _fmt_candidates(_cand_result())
    assert "2 rows" in out
    assert "🟢 ON" in out                      # armed true => ON


# ---------------------------------------------------------------------------
# _fmt_strategy
# ---------------------------------------------------------------------------
def test_strategy_detail_shows_summary_and_conditions():
    out = _fmt_strategy(_strategy_result())
    assert "**Main**" in out
    assert "Bull Put" in out or "bull_put" in out
    assert "short_delta" in out
    assert "spread_width" in out
    assert "15,000" in out          # budget (thousands-separated)


def test_strategy_run_days_on_one_line_with_bold_selected():
    line = _run_days_line([0, 1, 2, 3, 4])   # all weekdays
    assert "Mon" in line and "Fri" in line
    assert "[" in line and "]" in line
    assert "**Mon**" in line
    # a weekday not in the run list stays dimmed (not bold)
    dim = _run_days_line([0])                # Monday only
    assert "**Mon**" in dim
    assert "**Tue**" not in dim


def test_strategy_detail_uses_run_days_line():
    out = _fmt_strategy(_strategy_result())
    assert "[" in out
    assert "Mon" in out and "Wed" in out and "Fri" in out


# ---------------------------------------------------------------------------
# _fmt_status
# ---------------------------------------------------------------------------
def test_status_shows_readable_key_values():
    out = _fmt_status(_status_result())
    assert "RTH" in out
    assert "6,100.50" in out
    assert "14.2" in out


# ---------------------------------------------------------------------------
# _fmt_account
# ---------------------------------------------------------------------------
def test_account_shows_summary_and_count_not_raw_list():
    out = _fmt_account(_account_result())
    assert "100,000.00" in out
    assert "Net Liq" in out
    assert "1 position" in out


def test_account_shows_cash_and_excess_margin():
    out = _fmt_account(_account_result())
    assert "40,000.00" in out              # TotalCashValue -> Cash
    assert "Cash" in out
    assert "25,000.00" in out              # ExcessLiquidity -> Excess margin
    assert "Excess" in out
    assert "-500.00" in out                # UnrealizedPnL
    assert "1,200.00" in out               # RealizedPnL


def test_account_renders_summary_as_kv_not_dict_lines():
    out = _fmt_account(_account_result())
    assert "TotalCashValue:" not in out
    assert "NetLiquidation:" not in out


# ---------------------------------------------------------------------------
# _fmt_orders
# ---------------------------------------------------------------------------
def test_orders_shows_count_and_rows():
    out = _fmt_orders(_orders_result())
    assert "2 open orders" in out
    assert "BUY" in out and "SELL" in out
    assert "Submitted" in out or "Filled" in out


# ---------------------------------------------------------------------------
# _fmt_positions
# ---------------------------------------------------------------------------
def test_positions_shows_count_and_contracts():
    out = _fmt_positions(_positions_result())
    assert "2 positions" in out
    assert "SPX" in out


def test_positions_lists_full_option_name():
    out = _fmt_positions(_positions_result())
    assert "SPX Jun26 7700 CALL" in out
    assert "QCOM" in out


def test_positions_shows_avg_cost_and_unrealized_pnl():
    out = _fmt_positions(_positions_result())
    assert "1.85" in out                  # SPX averageCost
    assert "-40.00" in out                # SPX unrealizedPNL
    assert "175.20" in out                # QCOM averageCost
    assert "320.00" in out                # QCOM unrealizedPNL


# ---------------------------------------------------------------------------
# _fmt_action (arm/disarm/killswitch/place)
# ---------------------------------------------------------------------------
def test_action_arm_renders_armed_badge():
    out = _fmt_action("arm", {"ok": True, "name": "bp", "armed": True,
                              "auto_execute": False, "kill_switch": False})
    assert "**bp**" in out
    assert "🟢 ON" in out


def test_action_killswitch_renders_state():
    out = _fmt_action("killswitch", {"ok": True, "kill_switch": True})
    assert "🛑" in out
    assert "OFF" not in out


def test_action_place_refusal_renders_message():
    out = _fmt_action("place", {"ok": False,
                                "message": "Order placement is not allowed from Discord. Open the web UI."})
    assert "web UI" in out


# ---------------------------------------------------------------------------
# _fmt_alert (WebSocket broadcast alerts)
# ---------------------------------------------------------------------------
def test_alert_order_status_is_readable_not_dict():
    out = _fmt_alert("order_status", {"status": "Filled", "orderId": 7})
    assert "7" in out
    assert "Filled" in out
    assert "'orderId'" not in out        # not a raw dict dump


def test_alert_connection_transition():
    out = _fmt_alert("connection", {"connected": True})
    assert "Connected" in out or "connected" in out


def test_alert_kill_switch_uses_symbol():
    out = _fmt_alert("kill_switch", {"kill_switch": True})
    assert "🛑" in out


def test_alert_exit_reason():
    out = _fmt_alert("strategy_exit", {"name": "Main", "event": "take_profit"})
    assert "Main" in out
    assert "take_profit" in out


# ---------------------------------------------------------------------------
# Liquidation: position identity + close-order payload (pure)
# ---------------------------------------------------------------------------
def test_position_key_is_stable_discord_safe():
    c = {"symbol": "SPX", "secType": "OPT", "expiry": "20260626",
         "strike": 7700.0, "right": "C"}
    assert _position_key(c) == "SPX-OPT-20260626-7700-C"


def test_liquidation_payload_close_short_position_buys_back():
    # short option position (position < 0) -> BUY to close
    p = _liquidation_payload(
        {"symbol": "SPX", "secType": "OPT", "expiry": "20260626",
         "strike": 7700.0, "right": "C"},
        -4)
    assert p is not None
    leg = p["legs"][0]
    assert leg["action"] == "BUY" and leg["qty"] == 4
    assert leg["secType"] == "OPT"
    assert leg["expiry"] == "20260626" and leg["strike"] == 7700.0 and leg["right"] == "C"
    assert p["orderType"] == "LMT" and p["tif"] == "DAY"
    assert p["outsideRth"] is True and p["dynamicFill"] is True


def test_liquidation_payload_long_position_sells_to_close():
    p = _liquidation_payload({"symbol": "QCOM", "secType": "STK"}, 100)
    assert p is not None
    assert p["legs"][0]["action"] == "SELL" and p["legs"][0]["qty"] == 100
    assert "expiry" not in p["legs"][0]


def test_liquidation_payload_rejects_zero_and_unsupported():
    assert _liquidation_payload({"symbol": "SPX", "secType": "OPT"}, 0) is None
    assert _liquidation_payload({"symbol": "BUSD", "secType": "FUT"}, 2) is None
    assert _liquidation_payload(
        {"symbol": "SPX", "secType": "OPT", "expiry": "20260626",
         "strike": 7700.0, "right": ""}, -1) is None


def test_liquidatable_true_for_opt_and_stk():
    assert _liquidatable({"symbol": "SPX", "secType": "OPT",
                          "expiry": "20260626", "strike": 7700.0, "right": "C"}, -4)
    assert _liquidatable({"symbol": "QCOM", "secType": "STK"}, -100)
    assert not _liquidatable({"symbol": "SPX", "secType": "OPT"}, 0)


def test_positions_table_has_row_index_no_liq_column():
    out = _fmt_positions(_positions_result())
    # The clickable action lives on the buttons below, not as a wrapping liq column.
    assert "Liquidate" not in out
    assert "#" in out
    assert "contract" in out and "unrlzd pnl" in out


# ---------------------------------------------------------------------------
# _embed_color
# ---------------------------------------------------------------------------
def test_embed_color_red_on_error():
    assert _embed_color({"ok": False, "error": "not found"}) == 0xFF4136


def test_embed_color_red_when_kill_switch_on():
    assert _embed_color({"ok": True, "kill_switch": True}) == 0xFF4136


def test_embed_color_amber_for_empty_candidates():
    assert _embed_color(_empty_cand_result()) == 0xFFB347


def test_embed_color_green_for_ok_data():
    assert _embed_color(_cand_result()) == 0x00FF88
