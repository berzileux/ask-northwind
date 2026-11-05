from pathlib import Path

import pytest

import about
import pipeline
from about import ABOUT_SQL, ABOUT_TEXT, about_result, is_about_sql, load_about
from conftest import file_hash

ROOT = Path(__file__).parent


@pytest.fixture(scope="module", autouse=True)
def source_unchanged(db_path):
    before = file_hash(db_path)
    yield
    assert file_hash(db_path) == before


def fake(text):
    return lambda prompt: text


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'about_database' AS about",
        "select 'about_database' as about",
        "  SELECT   'about_database'   AS   about  ",
        "SELECT 'about_database' AS about;",
        "SELECT 'about_database' AS about;  ",
        "SELECT\n'about_database'\nAS\nabout\n",
        "Select 'About_Database' As About",
    ],
)
def test_the_sentinel_is_accepted_in_its_allowed_forms(sql):
    assert is_about_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'about_databases' AS about",
        "SELECT 'something else' AS about",
        "SELECT 'about_database' AS other",
        "SELECT 'about_database' AS about, 1 AS extra",
        "SELECT 'about_database' AS about -- note",
        "SELECT 'about_database' AS about /* c */",
        "SELECT 'about_database' AS about; SELECT 1",
        "SELECT 'about_database' AS about;;",
        "SELECT * FROM (SELECT 'about_database' AS about)",
        "SELECT 'about_database' AS about UNION ALL SELECT 'x'",
        "SELECT 'about_database' AS about FROM Customers",
        "WITH t AS (SELECT 'about_database' AS about) SELECT * FROM t",
        "",
        "   ",
        "SELECT 1",
        None,
        5,
    ],
)
def test_anything_else_is_not_the_sentinel(sql):
    assert not is_about_sql(sql)


def test_about_result_has_the_pipeline_shape():
    columns, rows = about_result()
    assert columns == ["about"]
    assert rows == [[ABOUT_TEXT]]


def test_the_text_is_the_file_content_trimmed():
    assert ABOUT_TEXT == (ROOT / "about.md").read_text().strip()
    assert ABOUT_SQL == "SELECT 'about_database' AS about"


def test_the_real_file_is_ascii_and_under_the_cap():
    raw = (ROOT / "about.md").read_bytes()
    raw.decode("ascii")
    assert 0 < len(raw) <= 1500


def test_loading_fails_loudly_for_a_missing_file(tmp_path):
    with pytest.raises(RuntimeError):
        load_about(tmp_path)


@pytest.mark.parametrize("content", [b"", b"   \n\n"])
def test_loading_fails_loudly_for_an_empty_file(tmp_path, content):
    (tmp_path / "about.md").write_bytes(content)
    with pytest.raises(RuntimeError):
        load_about(tmp_path)


def test_loading_fails_loudly_for_an_oversize_file(tmp_path):
    (tmp_path / "about.md").write_bytes(b"x" * 1501)
    with pytest.raises(RuntimeError):
        load_about(tmp_path)
    (tmp_path / "about.md").write_bytes(b"x" * 1500)
    assert load_about(tmp_path) == "x" * 1500


def test_loading_fails_loudly_for_non_ascii(tmp_path):
    (tmp_path / "about.md").write_bytes(("caf" + chr(233)).encode("utf-8"))
    with pytest.raises(RuntimeError):
        load_about(tmp_path)


def test_loading_trims_trailing_whitespace(tmp_path):
    (tmp_path / "about.md").write_text("Some text.  \n\n")
    assert load_about(tmp_path) == "Some text."


def test_ask_with_the_sentinel_gives_the_text(db_path):
    answer = ask_with(db_path, "SELECT 'about_database' AS about")
    assert answer.error_kind is None
    assert answer.columns == ["about"]
    assert answer.rows == [[ABOUT_TEXT]]
    assert answer.sql == "SELECT 'about_database' AS about"
    assert isinstance(answer.query_s, float)


def ask_with(db_path, sql, **kwargs):
    return pipeline.ask("What is this database about?", db_path, fake(sql), **kwargs)


def test_the_sentinel_with_a_semicolon_and_in_a_code_fence_gives_the_text(db_path):
    assert ask_with(db_path, "select 'about_database' as about;").rows == [[ABOUT_TEXT]]
    assert ask_with(db_path, "```sql\nSELECT 'about_database' AS about\n```").rows == [[ABOUT_TEXT]]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'something else' AS about",
        "SELECT 'about_database' AS about, 1 AS n",
        "SELECT * FROM (SELECT 'about_database' AS about)",
        "SELECT 'about_database' AS about -- x",
        "SELECT 'I cannot answer that from this database' AS message",
        "SELECT CompanyName FROM Customers",
    ],
)
def test_no_other_statement_returns_the_text(db_path, sql):
    answer = ask_with(db_path, sql)
    assert ABOUT_TEXT not in repr(answer.rows)
    assert ABOUT_TEXT not in repr(answer)


def test_a_stacked_statement_is_rejected_and_gives_no_text(db_path):
    answer = ask_with(db_path, "SELECT 'about_database' AS about; DROP TABLE Customers")
    assert answer.error_kind == "rejected"
    assert answer.rows == [] and ABOUT_TEXT not in repr(answer)


def test_the_text_is_not_substituted_when_the_sandbox_times_out_or_is_busy(db_path, monkeypatch):
    def late(sql, db, timeout_s=None):
        raise pipeline.SandboxTimeout("slow")

    monkeypatch.setattr(pipeline, "run_query", late)
    answer = ask_with(db_path, "SELECT 'about_database' AS about")
    assert answer.error_kind == "timeout" and answer.rows == []
    monkeypatch.undo()
    monkeypatch.setattr(pipeline, "WAIT_FOR_SLOT_S", 0.2)
    for _ in range(pipeline.GATE_SLOTS):
        pipeline.GATE.acquire()
    try:
        busy = ask_with(db_path, "SELECT 'about_database' AS about")
    finally:
        for _ in range(pipeline.GATE_SLOTS):
            pipeline.GATE.release()
    assert busy.error_kind == "busy" and busy.rows == []


def test_the_text_is_not_substituted_when_the_sandbox_rejects(db_path, monkeypatch):
    def refuse(sql, db, timeout_s=None):
        raise pipeline.SandboxError("no")

    monkeypatch.setattr(pipeline, "run_query", refuse)
    answer = ask_with(db_path, "SELECT 'about_database' AS about")
    assert answer.error_kind == "rejected" and answer.rows == []


def test_the_substitution_happens_after_the_run(db_path, monkeypatch):
    calls = []
    real = pipeline.run_query

    def spy(sql, db, timeout_s=None):
        calls.append(sql)
        return real(sql, db, timeout_s=timeout_s)

    monkeypatch.setattr(pipeline, "run_query", spy)
    ask_with(db_path, "SELECT 'about_database' AS about")
    assert calls == ["SELECT 'about_database' AS about"]


def test_the_answer_holds_no_model_written_text(db_path):
    answer = ask_with(db_path, "SELECT 'about_database' AS about")
    flattened = repr(answer.rows) + repr(answer.columns)
    assert "about_database" not in flattened
    assert answer.rows == [[ABOUT_TEXT]]


def test_bad_history_still_gives_input_for_an_about_question(db_path):
    seen = []

    def generate(prompt):
        seen.append(prompt)
        return "SELECT 'about_database' AS about"

    answer = pipeline.ask("What is this database about?", db_path, generate, history=[{"question": "x"}])
    assert answer.error_kind == "input" and seen == []


def test_history_is_still_used_for_an_about_question(db_path):
    seen = []

    def generate(prompt):
        seen.append(prompt)
        return "SELECT 'about_database' AS about"

    history = [{"question": "earlier", "sql": "SELECT 1", "columns": ["a"], "row_count": 1, "outcome": "answered"}]
    answer = pipeline.ask("What is this database about?", db_path, generate, history=history)
    assert answer.rows == [[ABOUT_TEXT]]
    assert "1. Question: earlier" in seen[0]


def test_the_module_loads_the_real_file_at_import():
    assert about.HERE == ROOT
    assert about.MAX_ABOUT_BYTES == 1500
