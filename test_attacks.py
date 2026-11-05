import sqlite3
import time

import pytest

from conftest import INJECTION_TEXT, file_hash
from sandbox import (
    SandboxRejected,
    SandboxTimeout,
    run_query,
)


@pytest.fixture(scope="module", autouse=True)
def source_unchanged(db_path):
    before = file_hash(db_path)
    yield
    assert file_hash(db_path) == before


REJECTED = {
    "stacked_statement": "SELECT 1; DROP TABLE Customers",
    "attach_memory": "ATTACH DATABASE ':memory:' AS other",
    "attach_file": "ATTACH DATABASE '/tmp/ask_northwind_attack.db' AS other",
    "pragma_table_info": "PRAGMA table_info(Customers)",
    "pragma_writable_schema": "PRAGMA writable_schema=1",
    "pragma_query_only_off": "PRAGMA query_only=OFF",
    "load_extension": "SELECT load_extension('anything')",
    "readfile": "SELECT readfile('/etc/passwd')",
    "writefile": "SELECT writefile('x','y')",
    "drop_table": "DROP TABLE Customers",
    "delete": "DELETE FROM Customers",
    "update": "UPDATE Customers SET ContactName='x'",
    "insert": "INSERT INTO Customers (CustomerID) VALUES ('ZZZ')",
    "create_table": "CREATE TABLE t (a)",
    "create_temp_table": "CREATE TEMP TABLE t (a)",
    "create_view": "CREATE VIEW v AS SELECT 1",
    "create_trigger": (
        "CREATE TRIGGER trg AFTER INSERT ON Customers BEGIN SELECT 1; END"
    ),
    "alter_table": "ALTER TABLE Customers ADD COLUMN x TEXT",
    "vacuum": "VACUUM",
    "reindex": "REINDEX",
    "begin": "BEGIN",
    "commit": "COMMIT",
    "savepoint": "SAVEPOINT sp",
    "mixed_case_attach": "aTtAcH DATABASE ':memory:' AS other",
    "comment_before_attach": "/**/ ATTACH DATABASE ':memory:' AS other",
    "newline_before_drop": "\nDROP TABLE Customers",
    "pragma_function": "SELECT * FROM pragma_table_info('Customers')",
    "zeroblob_too_big": "SELECT zeroblob(2000000000)",
    "string_too_big": "SELECT replace(printf('%500000s','a'),' ','bbb')",
    "sql_too_long": "SELECT 1 /* " + "x" * 10000 + " */",
}


@pytest.mark.parametrize("sql", REJECTED.values(), ids=REJECTED.keys())
def test_rejected(db_path, sql):
    with pytest.raises(SandboxRejected):
        run_query(sql, db_path)


@pytest.mark.parametrize(
    "value", [None, "", "   ", 1, b"SELECT 1"], ids=["none", "empty", "blank", "int", "bytes"]
)
def test_bad_input_type(db_path, value):
    with pytest.raises(SandboxRejected):
        run_query(value, db_path)


def test_endless_recursive_cte_times_out(db_path):
    sql = (
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
        "SELECT count(*) FROM c"
    )
    start = time.monotonic()
    with pytest.raises(SandboxTimeout):
        run_query(sql, db_path, timeout_s=0.5)
    assert time.monotonic() - start < 3


def test_four_way_cross_join_times_out(db_path):
    sql = (
        "SELECT count(*) FROM OrderDetails a, OrderDetails b, "
        "OrderDetails c, OrderDetails d"
    )
    start = time.monotonic()
    with pytest.raises(SandboxTimeout):
        run_query(sql, db_path, timeout_s=0.5)
    assert time.monotonic() - start < 3


def test_plain_select_returns_expected_rows(db_path):
    sql = "SELECT CustomerID, CompanyName FROM Customers ORDER BY CustomerID LIMIT 3"
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    expected = source.execute(sql).fetchall()
    source.close()

    result = run_query(sql, db_path)

    assert result.columns == ["CustomerID", "CompanyName"]
    assert result.rows == expected
    assert result.truncated is False


def test_trailing_semicolon_works(db_path):
    result = run_query("SELECT 1 AS one;", db_path)
    assert result.columns == ["one"]
    assert result.rows == [(1,)]


def test_injection_text_is_returned_as_data(db_path, db_is_generated):
    sql = "SELECT ContactName FROM Customers WHERE ContactName LIKE '%DROP TABLE%'"
    result = run_query(sql, db_path)
    if db_is_generated:
        assert result.rows == [(INJECTION_TEXT,)]
    assert all(row == (INJECTION_TEXT,) for row in result.rows)

    # Nothing in the returned text was executed.
    still_there = run_query("SELECT count(*) FROM Customers", db_path)
    assert still_there.rows[0][0] > 0


def test_row_cap_truncates(db_path):
    result = run_query("SELECT * FROM OrderDetails", db_path, max_rows=5)
    assert len(result.rows) == 5
    assert result.truncated is True


def test_byte_cap_truncates(db_path):
    result = run_query("SELECT * FROM OrderDetails", db_path, max_bytes=50)
    assert result.truncated is True
    assert len(result.rows) < 300
