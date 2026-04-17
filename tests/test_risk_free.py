from unittest.mock import MagicMock, patch

from risk_free import fetch_sgov_no_risk_yield, get_risk_free_rate


def test_fetch_sgov_no_risk_yield_uses_yfinance_yield_plus_expense_ratio():
    with patch("risk_free.yf") as mock_yf:
        mock_ticker = MagicMock()
        mock_ticker.info = {
            "yield": 0.0399,
            "netExpenseRatio": 0.09,
        }
        mock_yf.Ticker.return_value = mock_ticker

        result = fetch_sgov_no_risk_yield()

    assert abs(result - 0.0408) < 1e-8


def test_fetch_sgov_no_risk_yield_requires_yield():
    with patch("risk_free.yf") as mock_yf:
        mock_ticker = MagicMock()
        mock_ticker.info = {
            "netExpenseRatio": 0.09,
        }
        mock_yf.Ticker.return_value = mock_ticker

        try:
            fetch_sgov_no_risk_yield()
            assert False, "Expected missing yield to raise"
        except ValueError as exc:
            assert "yield" in str(exc)


def test_fetch_sgov_no_risk_yield_requires_expense_ratio():
    with patch("risk_free.yf") as mock_yf:
        mock_ticker = MagicMock()
        mock_ticker.info = {
            "yield": 0.0399,
        }
        mock_yf.Ticker.return_value = mock_ticker

        try:
            fetch_sgov_no_risk_yield()
            assert False, "Expected missing netExpenseRatio to raise"
        except ValueError as exc:
            assert "netExpenseRatio" in str(exc)



def test_get_risk_free_rate_falls_back_to_default_when_fetch_fails():
    with patch("risk_free.fetch_sgov_no_risk_yield", side_effect=Exception("network error")):
        result = get_risk_free_rate(default=0.0123)

    assert result == 0.0123
