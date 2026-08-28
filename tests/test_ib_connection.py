# tests/test_ib_connection.py
import asyncio
import importlib

import pytest

import config
from app_state import create_app_state
from ib_connection import connect_ib


class FakeIb:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def connect(self, host, port, client_id, timeout=None):
        self.calls.append((host, port, client_id, timeout))
        if self.fail:
            raise ConnectionRefusedError("no gateway")


def test_connect_records_port_on_success(monkeypatch):
    monkeypatch.delenv("IB_PORT", raising=False)
    importlib.reload(config)
    state = create_app_state()
    assert state.ib_port == config.IB_PORT
    fib = FakeIb()
    asyncio.run(connect_ib(fib, state, port=4002))
    assert state.ib_port == 4002
    assert fib.calls[0][1] == 4002


def test_connect_failure_leaves_port_unchanged(monkeypatch):
    monkeypatch.delenv("IB_PORT", raising=False)
    importlib.reload(config)
    state = create_app_state()
    fib = FakeIb(fail=True)
    with pytest.raises(ConnectionRefusedError):
        asyncio.run(connect_ib(fib, state, port=4002))
    assert state.ib_port == config.IB_PORT
    assert state.connected is False
