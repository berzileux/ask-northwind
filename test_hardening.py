import pytest

from conftest import file_hash
from sandbox import Result, SandboxError, SandboxRejected, run_query


@pytest.fixture(scope="module", autouse=True)
def source_unchanged(db_path):
    before = file_hash(db_path)
    yield
    assert file_hash(db_path) == before


REJECTED = {
    "lone_surrogate": "SELECT '\ud800'",
    "lone_low_surrogate": "SELECT '\udfff'",
    "null_byte": "SELECT 1\x00; DROP TABLE Customers",
    "fullwidth_attach": "\uff21\uff34\uff34\uff21\uff23\uff28 DATABASE ':memory:' AS x",
    "detach": "DETACH DATABASE x",
    "analyze": "ANALYZE",
    "replace_into": "REPLACE INTO Customers (CustomerID) VALUES ('X')",
    "cte_insert": (
        "WITH x AS (SELECT 1) INSERT INTO Customers (CustomerID) SELECT * FROM x"
    ),
    "cte_delete": "WITH x AS (SELECT 1) DELETE FROM Customers",
    "explain_delete": "EXPLAIN DELETE FROM Customers",
    "create_virtual_table": "CREATE VIRTUAL TABLE v USING fts5(a)",
    "nested_parentheses": "SELECT " + "(1+" * 500 + "1" + ")" * 500,
    "compound_select_terms": " UNION ALL ".join(["SELECT 1"] * 20),
    "limit_overflow": "SELECT 1 LIMIT 99999999999999999999",
    "tab_before_drop": "\tDROP TABLE Customers",
    "vertical_tab_before_drop": "\x0bDROP TABLE Customers",
    "form_feed_before_drop": "\x0cDROP TABLE Customers",
}


@pytest.mark.parametrize("sql", REJECTED.values(), ids=REJECTED.keys())
def test_rejected(db_path, sql):
    with pytest.raises(SandboxRejected):
        run_query(sql, db_path)


FUZZ_INPUTS = {
    "null_byte": "\x00",
    "lone_surrogate": "\ud800",
    "single_quote": "'",
    "double_quote": '"',
    "line_comment": "--",
    "block_comment_open": "/*",
    "semicolons": ";;;",
    "bare_select": "SELECT",
    "right_to_left_override": "\u202e",
    "four_byte_emoji": "\U0001F600",
    "five_thousand_parentheses": "(" * 5000,
    "blank": "   ",
}


@pytest.mark.parametrize("sql", FUZZ_INPUTS.values(), ids=FUZZ_INPUTS.keys())
def test_only_result_or_sandbox_error(db_path, sql):
    try:
        outcome = run_query(sql, db_path)
    except SandboxError:
        return
    assert isinstance(outcome, Result)


def test_rejection_message_hides_original_exception(db_path):
    with pytest.raises(SandboxRejected) as caught:
        run_query("SELECT '\ud800'", db_path)

    original = caught.value.__cause__
    assert original is not None
    assert str(original) not in str(caught.value)
    assert type(original).__name__ not in str(caught.value)
