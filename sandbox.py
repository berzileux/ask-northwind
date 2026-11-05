import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SQL_LENGTH_LIMIT = 10000
VALUE_LENGTH_LIMIT = 1000000
EXPR_DEPTH_LIMIT = 50
COMPOUND_SELECT_LIMIT = 10

# The timeout is checked once per this many VM instructions. Chosen by
# measuring how far a query overshoots a 0.5 second timeout at each setting.
PROGRESS_STEPS = 100000

# Rows are fetched in small batches so a few huge values cannot be pulled
# into memory all at once before the byte cap is checked.
FETCH_BATCH = 50

_ALLOWED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
)


class SandboxError(Exception):
    pass


class SandboxRejected(SandboxError):
    pass


class SandboxTimeout(SandboxError):
    pass


class SandboxSetupError(SandboxError):
    pass


@dataclass
class Result:
    columns: list
    rows: list
    truncated: bool
    elapsed_s: float


def _authorizer(action, arg1, arg2, db_name, source):
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    # arg2 carries the function name for FUNCTION actions.
    if action == sqlite3.SQLITE_FUNCTION and str(arg2).lower() == "load_extension":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _open_sandbox(db_path, timeout_s=2.0):
    """Return a private in-memory copy of the database with every guard installed."""
    try:
        source_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        source = sqlite3.connect(source_uri, uri=True)
        conn = sqlite3.connect(":memory:")
        try:
            source.backup(conn)
        finally:
            source.close()

        # query_only must be set before the authorizer, which denies PRAGMA.
        conn.execute("PRAGMA query_only=ON")
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, VALUE_LENGTH_LIMIT)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, SQL_LENGTH_LIMIT)
        conn.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, EXPR_DEPTH_LIMIT)
        conn.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, COMPOUND_SELECT_LIMIT)

        conn.set_authorizer(_authorizer)

        deadline = time.monotonic() + timeout_s

        def abort_when_late():
            return 1 if time.monotonic() > deadline else 0

        conn.set_progress_handler(abort_when_late, PROGRESS_STEPS)
        return conn
    except Exception as exc:
        raise SandboxSetupError(f"sandbox setup failed: {exc}") from exc


def _read_rows(cursor, max_rows, max_bytes):
    rows = []
    size = 0
    truncated = False
    while len(rows) <= max_rows and not truncated:
        batch = cursor.fetchmany(min(FETCH_BATCH, max_rows + 1 - len(rows)))
        if not batch:
            break
        for row in batch:
            rows.append(row)
            size += len(str(row))
            if size > max_bytes:
                truncated = True
                break
    if len(rows) > max_rows:
        rows = rows[:max_rows]
        truncated = True
    return rows, truncated


def run_query(sql, db_path, timeout_s=2.0, max_rows=500, max_bytes=1000000):
    start = time.monotonic()
    outcome = "rejected"
    conn = None
    try:
        if not isinstance(sql, str) or not sql.strip():
            raise SandboxRejected("sql must be a non-empty string")

        conn = _open_sandbox(db_path, timeout_s)
        try:
            cursor = conn.execute(sql)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            rows, truncated = _read_rows(cursor, max_rows, max_bytes)
        except sqlite3.OperationalError as exc:
            # The progress handler is the only source of interrupts here.
            if "interrupted" in str(exc):
                outcome = "timeout"
                raise SandboxTimeout("query exceeded the time limit") from exc
            raise SandboxRejected(str(exc)) from exc
        except sqlite3.Error as exc:
            raise SandboxRejected(str(exc)) from exc
        except Exception as exc:
            # Failures outside sqlite3 (for example an unencodable character)
            # must reach the caller as a SandboxError. The detail goes to the
            # log only, so internals are not echoed back.
            outcome = f"rejected unexpected {type(exc).__name__}: {exc}"
            raise SandboxRejected("query could not be run") from exc

        outcome = "ok"
        return Result(columns, rows, truncated, time.monotonic() - start)
    finally:
        if conn is not None:
            conn.close()
        log.info(
            "sql=%r outcome=%s elapsed_s=%.3f", sql, outcome, time.monotonic() - start
        )
