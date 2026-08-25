"""Tests for the Log console: LogStoreHandler capture, push-loop broadcast, and
the set_tab:log backlog reply."""

import asyncio
import json
import logging

import pytest
from fastapi import WebSocketDisconnect

from app_state import create_app_state
from log_buffer import LogStoreHandler, log_push_loop
from ws_handler import websocket_endpoint


class FakeWS:
    """Minimal WebSocket: queues inbound messages, records outbound JSON."""

    def __init__(self, messages):
        self._queue = list(messages)
        self.sent = []

    async def accept(self):
        pass

    async def receive_text(self):
        if self._queue:
            return self._queue.pop(0)
        raise WebSocketDisconnect()

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    def sent_by_type(self, msg_type):
        return [m for m in self.sent if m.get("type") == msg_type]


async def _noop_broadcast(message):
    pass


@pytest.mark.asyncio
async def test_log_store_handler_increments_seq_and_formats():
    state = create_app_state()
    handler = LogStoreHandler(state)
    handler.emit(logging.LogRecord(
        name="server", level=logging.INFO, pathname="", lineno=0,
        msg="hello from %s", args=("spx",), exc_info=None))

    assert state.log_seq == 1
    assert len(state.log_buffer) == 1
    entry = state.log_buffer[0]
    assert entry["seq"] == 0
    assert entry["level"] == "INFO"
    assert entry["name"] == "server"
    assert "hello from spx" in entry["msg"]


@pytest.mark.asyncio
async def test_log_store_handler_bounds_buffer():
    state = create_app_state()
    handler = LogStoreHandler(state)
    for i in range(600):
        handler.emit(logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg=str(i), args=(), exc_info=None))

    # maxlen=500: the oldest 100 records are dropped, first kept seq is 100.
    assert len(state.log_buffer) == 500
    assert state.log_buffer[0]["seq"] == 100
    assert state.log_seq == 600


@pytest.mark.asyncio
async def test_log_push_loop_broadcasts_new_entries(monkeypatch):
    state = create_app_state()
    messages = []
    calls = {"n": 0}

    async def broadcast_fn(message):
        messages.append(message)

    async def sleep_and_append(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            entry = {"seq": state.log_seq, "ts": "10:00:00", "level": "INFO",
                     "name": "server", "msg": "hello"}
            state.log_seq += 1
            state.log_buffer.append(entry)
        else:
            raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", sleep_and_append)

    await log_push_loop(state, broadcast_fn)

    logs = [m for m in messages if m["type"] == "log"]
    assert len(logs) >= 1
    assert logs[0]["data"]["msg"] == "hello"


@pytest.mark.asyncio
async def test_log_push_loop_does_not_replay_preexisting_backlog(monkeypatch):
    """Entries already in the buffer when the push loop starts are NOT broadcast
    globally — clients fetch them via set_tab:log / log_history instead."""
    state = create_app_state()
    state.log_buffer.append({"seq": 0, "ts": "10:00:00", "level": "INFO",
                             "name": "server", "msg": "old"})
    state.log_seq = 1
    messages = []
    calls = {"n": 0}

    async def broadcast_fn(message):
        messages.append(message)

    async def sleep_once_then_cancel(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            return
        raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", sleep_once_then_cancel)

    await log_push_loop(state, broadcast_fn)

    # last_sent is state.log_seq - 1 == 0, and seq 0 is not > 0, so it is skipped.
    assert messages == []


@pytest.mark.asyncio
async def test_set_tab_log_sends_log_history(app_state):
    app_state.log_buffer.extend([
        {"seq": 0, "ts": "10:00:00", "level": "INFO", "name": "server", "msg": "hello"},
        {"seq": 1, "ts": "10:00:01", "level": "ERROR", "name": "chain_fetcher", "msg": "boom"},
    ])
    app_state.log_seq = 2
    ws = FakeWS(["set_tab:log"])

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    hist = ws.sent_by_type("log_history")
    assert len(hist) == 1
    assert hist[0]["data"][0]["msg"] == "hello"
    assert hist[0]["data"][1]["level"] == "ERROR"
