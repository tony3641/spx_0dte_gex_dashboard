import asyncio
from ib_connection import setup_vix_subscription, update_vix
import pytest


@pytest.mark.asyncio
async def test_vix_subscription_and_update(mock_ib, app_state):
    await setup_vix_subscription(mock_ib, app_state)
    assert app_state.vix_stream is not None
    update_vix(app_state)
    assert app_state.vix is not None and app_state.vix > 0
