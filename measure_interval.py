import sys
import threading
import time
from pathlib import Path

import sandbox
from measure_cost import REVENUE_BY_YEAR

DB_PATH = Path(__file__).parent / "northwind.db"

PROGRESS_STEP_CANDIDATES = (1000, 10000, 100000)
MAX_OVERSHOOT_S = 0.25
CROSS_JOIN_TIMEOUT_S = 0.5

# The real order-lines table has a space in its name; it is cross joined with
# itself four times so the query cannot finish inside the timeout.
CROSS_JOIN = (
    'SELECT count(*) FROM "Order Details" a, "Order Details" b, '
    '"Order Details" c, "Order Details" d'
)


def alone_runs():
    for run in range(1, 4):
        result = sandbox.run_query(REVENUE_BY_YEAR, DB_PATH, timeout_s=10)
        print(f"  revenue by year alone, run {run}: elapsed_s={result.elapsed_s:.3f}")


def cross_join_overshoot():
    start = time.monotonic()
    try:
        sandbox.run_query(CROSS_JOIN, DB_PATH, timeout_s=CROSS_JOIN_TIMEOUT_S)
        print("  cross join: finished without a timeout")
        return None
    except sandbox.SandboxTimeout:
        wall = time.monotonic() - start
        overshoot = wall - CROSS_JOIN_TIMEOUT_S
        print(
            f"  cross join: SandboxTimeout after wall_s={wall:.3f}, "
            f"overshoot_s={overshoot:.3f}"
        )
        return overshoot


def four_threads():
    barrier = threading.Barrier(4)
    lines = []

    def worker(index):
        barrier.wait()
        try:
            result = sandbox.run_query(REVENUE_BY_YEAR, DB_PATH, timeout_s=10)
            lines.append((index, f"elapsed_s={result.elapsed_s:.3f} raised=no"))
        except Exception as exc:
            lines.append((index, f"raised={type(exc).__name__}: {exc}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for index, text in sorted(lines):
        print(f"  four threads, thread {index}: {text}")


def main():
    if not DB_PATH.exists():
        print("northwind.db does not exist in the repo root: measurement not run")
        return 2

    overshoots = {}
    for steps in PROGRESS_STEP_CANDIDATES:
        sandbox.PROGRESS_STEPS = steps
        print(f"PROGRESS_STEPS={steps}")
        alone_runs()
        overshoots[steps] = cross_join_overshoot()
        four_threads()

    qualifying = [
        steps
        for steps, over in overshoots.items()
        if over is not None and over <= MAX_OVERSHOOT_S
    ]
    print(f"candidates within overshoot {MAX_OVERSHOOT_S}s: {qualifying}")
    print(f"chosen by the selection rule: {max(qualifying) if qualifying else 1000}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
