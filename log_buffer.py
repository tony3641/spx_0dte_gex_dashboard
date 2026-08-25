"""Framework log capture + WebSocket push for the Log console tab.

A root-logger handler appends each record to the bounded in-memory ring
buffer on AppState.log_buffer. A background log_push_loop drains entries it
has not yet sent and broadcasts them as {"type": "log", ...} so any client's
Log tab renders them live. A client joining late requests the backlog via the
set_tab:log handler in ws_handler, which returns the current buffer.

The handler may run from arbitrary threads (IB callbacks, asyncio loop
threads, etc.), so seq assignment and buffer appends share a lock; the drain
helper copies the current snapshot under the same lock.
"""

import asyncio
import logging
import threading
import time

_lock = threading.Lock()


def _stamp(state, record, message: str) -> dict:
    """Build a serializable log entry keyed by a monotonically increasing seq."""
    with _lock:
        entry = {
            "seq": state.log_seq,
            "ts": time.strftime("%H:%M:%S"),
            "level": record.levelname,
            "name": record.name,
            "msg": message,
        }
        state.log_seq += 1
        state.log_buffer.append(entry)
    return entry


class LogStoreHandler(logging.Handler):
    """logging.Handler that stores formatted records in the state ring buffer."""

    def __init__(self, state):
        super().__init__()
        self.setLevel(logging.DEBUG)
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S"))
        self._state = state

    def emit(self, record: logging.LogRecord):
        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return
        _stamp(self._state, record, message)


def new_log_entries(state, last_seq: int):
    """Return (entries, last_seq) for records newer than last_seq, then update it.

    Used by log_push_loop to broadcast only the delta. A thread-safe snapshot
    is taken under the same lock the handler uses so an in-flight append cannot
    corrupt iteration.
    """
    with _lock:
        entries = [e for e in state.log_buffer if e["seq"] > last_seq]
    if entries:
        last_seq = entries[-1]["seq"]
    return entries, last_seq


async def log_push_loop(state, broadcast_fn):
    """Broadcast new log entries to all connected clients on a short cadence."""
    # Start one behind the current high-water mark: the first record appended
    # after this loop begins gets seq == state.log_seq and must NOT be skipped.
    last_sent = state.log_seq - 1
    while True:
        try:
            await asyncio.sleep(0.5)
            entries, last_sent = new_log_entries(state, last_sent)
            for entry in entries:
                await broadcast_fn({"type": "log", "data": entry})
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.getLogger("log_buffer").error(f"log_push_loop error: {e}")
            await asyncio.sleep(2)
