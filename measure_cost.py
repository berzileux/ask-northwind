import inspect
import resource
import sys
import threading
from pathlib import Path

import sandbox

DB_PATH = Path(__file__).parent / "northwind.db"

REVENUE_BY_YEAR = (
    "SELECT strftime('%Y', O.OrderDate) AS Year, "
    "SUM(OD.UnitPrice * OD.Quantity * (1 - OD.Discount)) AS TotalRevenue "
    'FROM Orders AS O JOIN "Order Details" AS OD ON O.OrderID = OD.OrderID '
    "GROUP BY Year"
)

TOP_PRODUCTS = (
    'SELECT P.ProductName FROM Products AS P JOIN "Order Details" AS OD '
    "ON P.ProductID = OD.ProductID GROUP BY P.ProductID "
    "ORDER BY SUM(OD.UnitPrice * OD.Quantity * (1 - OD.Discount)) DESC LIMIT 5"
)

timeouts_seen = []


def peak_kb():
    # On Linux ru_maxrss is reported in kilobytes.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def timed_run(label, sql):
    try:
        result = sandbox.run_query(sql, DB_PATH)
    except sandbox.SandboxTimeout:
        timeouts_seen.append(label)
        print(f"{label}: SandboxTimeout")
        return None
    return result.elapsed_s


def repeat(label, sql):
    elapsed = []
    for run in range(1, 4):
        value = timed_run(f"{label} run {run}", sql)
        if value is not None:
            elapsed.append(value)
            print(f"{label} run {run}: elapsed_s={value:.3f}")
    if elapsed:
        print(f"{label} max elapsed_s={max(elapsed):.3f}")
    return elapsed


def concurrent_runs():
    barrier = threading.Barrier(4)
    lines = []

    def worker(index):
        barrier.wait()
        try:
            result = sandbox.run_query(REVENUE_BY_YEAR, DB_PATH)
            lines.append((index, f"elapsed_s={result.elapsed_s:.3f}"))
        except Exception as exc:
            if isinstance(exc, sandbox.SandboxTimeout):
                timeouts_seen.append(f"thread {index}")
            lines.append((index, f"{type(exc).__name__}: {exc}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for index, text in sorted(lines):
        print(f"thread {index}: {text}")


def main():
    if not DB_PATH.exists():
        print("northwind.db does not exist in the repo root: measurement not run")
        return 2

    print(f"1. baseline peak memory kb: {peak_kb()}")

    count = timed_run("2. count of order lines", 'SELECT COUNT(*) FROM "Order Details"')
    print(f"2. count query elapsed_s: {count}")
    print(f"2. peak memory kb after count: {peak_kb()}")

    print("3. revenue by year, three runs")
    repeat("revenue by year", REVENUE_BY_YEAR)
    print("3. top 5 products, three runs")
    repeat("top 5 products", TOP_PRODUCTS)

    print("4. four concurrent revenue by year runs")
    concurrent_runs()
    print(f"4. peak memory kb after concurrent runs: {peak_kb()}")

    default_timeout = inspect.signature(sandbox.run_query).parameters["timeout_s"].default
    print(f"5. SandboxTimeout raised in any run: {'yes' if timeouts_seen else 'no'}")
    if timeouts_seen:
        print(f"5. timed out: {timeouts_seen}")
    print(f"5. sandbox default timeout_s: {default_timeout}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
