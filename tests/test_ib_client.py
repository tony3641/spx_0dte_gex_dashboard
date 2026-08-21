# tests/test_ib_client.py
import asyncio
import pytest
from unittest import mock
from ibapi.client import EClient

from ib_client import IBClient


@pytest.mark.asyncio
async def test_connect_async_waits_for_next_valid_id(monkeypatch):
    client = IBClient()
    monkeypatch.setattr(EClient, "connect", lambda self, *a, **k: True)
    monkeypatch.setattr(EClient, "run", lambda self: None)
    task = asyncio.create_task(client.connect("127.0.0.1", 7497, 1, timeout=1))
    await asyncio.sleep(0.01)
    client.nextValidId(42)            # simulate the socket-thread callback
    await asyncio.wait_for(task, timeout=2)
    assert client._next_order_id == 42
    assert client.connected


@pytest.mark.asyncio
async def test_connect_async_times_out():
    client = IBClient()
    with pytest.raises(ConnectionError):
        with mock.patch.object(EClient, "connect", return_value=True), \
             mock.patch.object(EClient, "run", lambda self: None):
            await client.connect("127.0.0.1", 7497, 1, timeout=0.05)


def test_disconnect_sets_connected_false(monkeypatch):
    client = IBClient()
    client.connected = True
    disconnect_called = False
    def fake_disconnect(self):
        nonlocal disconnect_called
        disconnect_called = True
    monkeypatch.setattr(EClient, "disconnect", fake_disconnect)
    client.disconnect()
    assert not client.connected
    assert disconnect_called
