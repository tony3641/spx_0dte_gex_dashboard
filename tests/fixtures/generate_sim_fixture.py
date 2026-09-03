"""Generate the committed 5-min fixture CSV used by sim tests (hermetic, no network).

Run:  python tests/fixtures/generate_sim_fixture.py
Writes: tests/fixtures/sim_bars_5m.csv
"""
import os
from datetime import datetime, timedelta

import numpy as np

OUT = os.path.join(os.path.dirname(__file__), "sim_bars_5m.csv")
BARS_PER_DAY = 78            # 09:30-16:00 at 5 min
DAYS = 10


def main() -> None:
    rng = np.random.default_rng(7)
    lines = ["timestamp,open,high,low,close,volume"]
    s = 6000.0
    sigma2 = (0.0004 / 78) ** 2 * 100  # per-bar var ~ (0.045% of spot)^2-ish
    omega, alpha, gamma, beta = 1e-10, 0.05, 0.08, 0.85
    day = datetime(2026, 6, 1)
    made = 0
    while made < DAYS:
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue
        t0 = day.replace(hour=9, minute=30)
        for b in range(BARS_PER_DAY):
            z = rng.standard_t(6) / np.sqrt(6 / 4)
            eps = np.sqrt(sigma2) * z
            o = s
            s = s * float(np.exp(eps))
            hi = max(o, s) * (1 + abs(rng.normal(0, 0.0002)))
            lo = min(o, s) * (1 - abs(rng.normal(0, 0.0002)))
            ts = (t0 + timedelta(minutes=5 * (b + 1))).strftime("%Y-%m-%dT%H:%M:%S-04:00")
            lines.append(f"{ts},{o:.2f},{hi:.2f},{lo:.2f},{s:.2f},{int(rng.integers(500, 5000))}")
            sigma2 = omega + alpha * eps ** 2 + gamma * eps ** 2 * (eps < 0) + beta * sigma2
        day += timedelta(days=1)
        made += 1
    with open(OUT, "w", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {OUT}: {len(lines) - 1} rows")


if __name__ == "__main__":
    main()
