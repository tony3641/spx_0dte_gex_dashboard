import io
from unittest.mock import MagicMock, patch

from risk_free import fetch_sgov_7_day_yield, get_risk_free_rate


def test_fetch_sgov_7_day_yield_parses_yahoo_html():
    html = """
      <tr>
        <td>7 Day Yield</td>
        <td><span>0.92%</span></td>
      </tr>
    """

    mock_response = MagicMock()
    mock_response.read.return_value = html.encode("utf-8")
    with patch("risk_free.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = mock_response
        result = fetch_sgov_7_day_yield()

    assert abs(result - 0.0092) < 1e-8


def test_get_risk_free_rate_falls_back_to_default_when_fetch_fails():
    with patch("risk_free.fetch_sgov_7_day_yield", side_effect=Exception("network error")):
        result = get_risk_free_rate(default=0.0123)

    assert result == 0.0123
