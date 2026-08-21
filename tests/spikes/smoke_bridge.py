"""Standalone bridge smoke: connect + SPX subscription against paper (TWS 7497).

Throwaway — full end-to-end is finalized in Task 17 (server.py wiring). This
smokes the native IBClient + ported ib_connection entry points directly.

Run:  python tests/spikes/smoke_bridge.py
Requires: TWS/paper running on 127.0.0.1:7497 (default IB_CLIENT_ID).
"""
import asyncio
import os
import sys

# ibapi lives in the TWS install; the repo modules live in the project root.
sys.path.insert(0, r"C:\TWS API\source\pythonclient")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ib_client import IBClient          # noqa: E402
from ib_connection import connect_ib, setup_spx_subscription  # noqa: E402
from app_state import AppState          # noqa: E402


async def main():
    ib = IBClient()
    state = AppState()
    try:
        await connect_ib(ib, state)
    except Exception as e:
        print(f"[smoke] FAILED to connect: {e}")
        return 1
    print(f"[smoke] connected (state.connected={state.connected})")

    await setup_spx_subscription(ib, state)
    s = getattr(state, "spx_stream", None)
    print(f"[smoke] spx_stream reqId={getattr(s, 'req_id', None)}")

    for _ in range(20):
        s = getattr(state, "spx_stream", None)
        if s is not None and (s.bid or s.ask or s.last):
            print(f"[smoke] SPX quote bid={s.bid} ask={s.ask} last={s.last}")
            break
        if s is not None and s.received_any_tick():
            # Paper accounts without a live SPX index subscription still receive
            # the previous close tick. Surface it as partial evidence.
            print(f"[smoke] SPX tick received (bid={s.bid} ask={s.ask} last={s.last} "
                  f"close={s.close}) — no live BBO on this data subscription")
            break
        await asyncio.sleep(0.5)
    else:
        print("[smoke] FAILED: no SPX tick within ~10s")
        ib.disconnect()
        return 1

    ib.disconnect()
    print("[smoke] disconnected")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
