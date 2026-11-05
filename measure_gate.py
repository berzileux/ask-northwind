import resource
import sys
import threading
import time
from pathlib import Path

import pipeline
from measure_cost import REVENUE_BY_YEAR

DB_PATH = Path(__file__).parent / "northwind.db"


def fake_generate(prompt):
    # The model is never called; the same query comes back every time.
    return REVENUE_BY_YEAR


def four_calls():
    barrier = threading.Barrier(4)
    lines = []
    started = []

    def worker(index):
        barrier.wait()
        begin = time.monotonic()
        started.append(begin)
        answer = pipeline.ask("revenue by year?", str(DB_PATH), fake_generate)
        wall = time.monotonic() - begin
        lines.append(
            (
                index,
                f"error_kind={answer.error_kind} error={answer.error} "
                f"row_count={len(answer.rows)} wall_s={wall:.3f}",
            )
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    total = time.monotonic() - min(started)
    for index, text in sorted(lines):
        print(f"  call {index}: {text}")
    print(f"  total wall_s={total:.3f}")
    print(f"  peak memory kb={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}")


def main():
    if not DB_PATH.exists():
        print("northwind.db does not exist in the repo root: measurement not run")
        return 2

    print(
        f"default run: GATE_SLOTS={pipeline.GATE_SLOTS} "
        f"WAIT_FOR_SLOT_S={pipeline.WAIT_FOR_SLOT_S} "
        f"QUERY_TIMEOUT_S={pipeline.QUERY_TIMEOUT_S}"
    )
    four_calls()

    original = pipeline.WAIT_FOR_SLOT_S
    pipeline.WAIT_FOR_SLOT_S = 0.5
    try:
        print("short wait run: WAIT_FOR_SLOT_S=0.5")
        four_calls()
    finally:
        pipeline.WAIT_FOR_SLOT_S = original
    return 0


if __name__ == "__main__":
    sys.exit(main())
