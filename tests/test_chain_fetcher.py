from dataclasses import dataclass
from types import SimpleNamespace
import math

from chain_fetcher import _ticker_to_option_data
from gex_calculator import compute_gex, OptionData, _bsm_gamma


@dataclass
class DummyContract:
    conId: int = 0
    symbol: str = ''
    secType: str = ''
    lastTradeDateOrContractMonth: str = ''
    strike: float = 0.0
    right: str = ''
    multiplier: str = '100'
    currency: str = 'USD'
    exchange: str = 'SMART'
    localSymbol: str = ''
    tradingClass: str = ''
    comboLegs: list = None


@dataclass
class DummyTicker:
    bid: float = -1
    ask: float = -1
    last: float = -1
    bidSize: float = 0
    askSize: float = 0
    volume: float = 0
    callOpenInterest: float = -1
    putOpenInterest: float = -1
    impliedVolatility: float = None
    modelGreeks: any = None
    lastGreeks: any = None
    bidGreeks: any = None
    askGreeks: any = None
    contract: any = None


def test_ticker_to_option_data_put_open_interest_falls_back_to_call_field():
    contract = DummyContract(
        symbol='SPX',
        secType='OPT',
        lastTradeDateOrContractMonth='20260410',
        strike=6800.0,
        right='P',
        exchange='SMART',
        tradingClass='SPXW',
    )
    ticker = DummyTicker(
        bid=1.0,
        ask=1.2,
        last=1.1,
        contract=contract,
        callOpenInterest=1234,
        putOpenInterest=-1,
        modelGreeks=SimpleNamespace(gamma=0.05, delta=-0.3, impliedVol=0.18),
    )

    opt_data = _ticker_to_option_data(ticker)

    assert opt_data is not None
    assert opt_data.open_interest == 1234


def test_ticker_to_option_data_call_open_interest_falls_back_to_put_field():
    contract = DummyContract(
        symbol='SPX',
        secType='OPT',
        lastTradeDateOrContractMonth='20260410',
        strike=6800.0,
        right='C',
        exchange='SMART',
        tradingClass='SPXW',
    )
    ticker = DummyTicker(
        bid=1.0,
        ask=1.2,
        last=1.1,
        contract=contract,
        callOpenInterest=-1,
        putOpenInterest=567,
        modelGreeks=SimpleNamespace(gamma=0.05, delta=0.7, impliedVol=0.18),
    )

    opt_data = _ticker_to_option_data(ticker)

    assert opt_data is not None
    assert opt_data.open_interest == 567


def test_ticker_to_option_data_uses_bid_greeks_when_model_and_last_missing():
    contract = DummyContract(
        symbol='SPX',
        secType='OPT',
        lastTradeDateOrContractMonth='20260410',
        strike=6825.0,
        right='C',
        exchange='SMART',
        tradingClass='SPXW',
    )
    ticker = DummyTicker(
        bid=1.0,
        ask=1.2,
        last=1.1,
        contract=contract,
        callOpenInterest=100,
        bidGreeks=SimpleNamespace(gamma=0.01234, delta=0.55, impliedVol=0.199),
    )

    opt_data = _ticker_to_option_data(ticker)

    assert opt_data is not None
    assert abs(opt_data.gamma - 0.01234) < 1e-9
    assert abs(opt_data.delta - 0.55) < 1e-9
    assert abs(opt_data.implied_vol - 0.199) < 1e-9


def test_ticker_to_option_data_normalizes_percent_like_iv_values():
    contract = DummyContract(
        symbol='SPX',
        secType='OPT',
        lastTradeDateOrContractMonth='20260410',
        strike=6850.0,
        right='P',
        exchange='SMART',
        tradingClass='SPXW',
    )
    ticker = DummyTicker(
        bid=1.0,
        ask=1.2,
        last=1.1,
        contract=contract,
        putOpenInterest=100,
        modelGreeks=SimpleNamespace(gamma=0.02, delta=-0.4, impliedVol=18.3),
    )

    opt_data = _ticker_to_option_data(ticker)

    assert opt_data is not None
    assert abs(opt_data.implied_vol - 0.183) < 1e-9


# ---------------------------------------------------------------------------
# BSM gamma fallback in compute_gex
# ---------------------------------------------------------------------------

def _make_option(strike, right, oi, gamma=None, iv=None):
    return OptionData(
        strike=strike, right=right, open_interest=oi,
        gamma=gamma, implied_vol=iv,
    )


def test_compute_gex_uses_bsm_gamma_when_ib_gamma_missing():
    """When IB gamma is None but IV is present, BSM gamma should be used and GEX != 0."""
    spot = 6800.0
    tte = 30.0 / (390.0 * 252.0)  # ~30 min of 0DTE
    call = _make_option(6800, 'C', oi=1000, gamma=None, iv=0.20)
    put  = _make_option(6800, 'P', oi=1000, gamma=None, iv=0.20)

    result = compute_gex([call, put], spot, time_to_expiry_years=tte)

    assert result.total_call_gex != 0.0, "Call GEX must be non-zero when BSM fallback is used"
    assert result.total_put_gex != 0.0, "Put GEX must be non-zero when BSM fallback is used"
    assert len(result.gex_by_strike) > 0

    # Verify the BSM gamma formula is plausible
    bsm_g = _bsm_gamma(spot, 6800, tte, 0.053, 0.20)
    assert bsm_g is not None and bsm_g > 0
    expected_call_gex = bsm_g * 1000 * 100 * spot * 0.01
    assert abs(result.total_call_gex - expected_call_gex) < 0.01


def test_compute_gex_ib_gamma_takes_precedence_over_bsm():
    """When IB provides gamma it should be used, not BSM."""
    spot = 6800.0
    tte = 30.0 / (390.0 * 252.0)
    ib_gamma = 0.99  # deliberately unrealistic to distinguish from BSM result
    call = _make_option(6800, 'C', oi=100, gamma=ib_gamma, iv=0.20)

    result = compute_gex([call], spot, time_to_expiry_years=tte)

    expected = ib_gamma * 100 * 100 * spot * 0.01
    assert abs(result.total_call_gex - expected) < 0.01


def test_compute_gex_no_gex_when_gamma_and_iv_both_missing():
    """With no gamma and no IV, GEX should be 0 (cannot compute)."""
    spot = 6800.0
    tte = 30.0 / (390.0 * 252.0)
    call = _make_option(6800, 'C', oi=500, gamma=None, iv=None)

    result = compute_gex([call], spot, time_to_expiry_years=tte)

    assert result.total_call_gex == 0.0
