import json

import pytest

import evaluate
from evaluate import compare_rows, rows_match


def test_rows_match_still_compares_every_column():
    assert not rows_match([("highest", "a", 1.0)], [("most", "a", 1.0)])


def test_without_ignore_columns_a_label_difference_fails():
    assert not compare_rows([("highest", "a", 1.0)], [("most", "a", 1.0)])
    assert not compare_rows([("highest", "a", 1.0)], [("most", "a", 1.0)], [])
    assert not compare_rows([("highest", "a", 1.0)], [("most", "a", 1.0)], None)


def test_ignore_columns_drops_the_column_on_both_sides():
    reference = [("highest", "a", 1.0), ("lowest", "b", 2.0)]
    actual = [("most", "a", 1.001), ("least", "b", 2.0)]
    assert compare_rows(reference, actual, [0])


def test_ignored_column_does_not_hide_a_real_difference():
    reference = [("highest", "a", 1.0)]
    actual = [("most", "z", 1.0)]
    assert not compare_rows(reference, actual, [0])


@pytest.mark.parametrize("bad", [-1, 3, 1.5, "0", True, None])
def test_bad_indexes_raise_a_clear_error(bad):
    with pytest.raises(ValueError):
        compare_rows([("a", "b", "c")], [("a", "b", "c")], [bad])


def run_main(monkeypatch, tmp_path, entries, answers):
    (tmp_path / "known_answers.json").write_text(json.dumps(entries))
    monkeypatch.setattr(evaluate, "HERE", tmp_path)
    monkeypatch.setattr(evaluate, "PAUSE_S", 0)

    class Answer:
        def __init__(self, rows):
            self.error = None
            self.error_kind = None
            self.rows = rows
            self.sql = "SELECT 1"
            self.elapsed_s = 0.0

    monkeypatch.setattr(evaluate, "ask", lambda q, db: Answer(answers[q]))
    monkeypatch.setattr(
        evaluate,
        "run_query",
        lambda sql, db: type("R", (), {"rows": [("highest", "a", 1.0)]})(),
    )
    return evaluate.main(["evaluate.py", "unused.db"])


def test_main_passes_with_ignore_columns_and_fails_without(monkeypatch, tmp_path, capsys):
    entries = [
        {"question": "q1", "reference_sql": "x", "ignore_columns": [0]},
        {"question": "q2", "reference_sql": "x"},
    ]
    answers = {"q1": [("most", "a", 1.0)], "q2": [("most", "a", 1.0)]}
    run_main(monkeypatch, tmp_path, entries, answers)
    out = capsys.readouterr().out
    assert "pass  0.00s  q1" in out
    assert "fail  0.00s  q2" in out
    assert "correct 1 of 2" in out


def test_a_fail_prints_the_model_sql_and_first_rows(monkeypatch, tmp_path, capsys):
    entries = [{"question": "q2", "reference_sql": "x"}]
    run_main(monkeypatch, tmp_path, entries, {"q2": [("most", "a", 1.0), ("b", 2, 3), ("c", 3, 4)]})
    out = capsys.readouterr().out
    assert "model sql: SELECT 1" in out
    assert "model rows: [('most', 'a', 1.0), ('b', 2, 3)]" in out
    assert "expected rows: [('highest', 'a', 1.0)]" in out


def test_pause_is_between_questions_not_before_the_first(monkeypatch, tmp_path):
    waits = []
    monkeypatch.setattr(evaluate.time, "sleep", lambda s: waits.append(s))
    entries = [{"question": f"q{i}", "reference_sql": "x"} for i in range(3)]
    answers = {f"q{i}": [("highest", "a", 1.0)] for i in range(3)}
    (tmp_path / "known_answers.json").write_text(json.dumps(entries))
    monkeypatch.setattr(evaluate, "HERE", tmp_path)
    monkeypatch.setattr(evaluate, "PAUSE_S", 2.0)

    class Answer:
        error = None
        error_kind = None
        sql = ""
        elapsed_s = 0.0

        def __init__(self, rows):
            self.rows = rows

    monkeypatch.setattr(evaluate, "ask", lambda q, db: Answer(answers[q]))
    monkeypatch.setattr(
        evaluate, "run_query", lambda sql, db: type("R", (), {"rows": [("highest", "a", 1.0)]})()
    )
    evaluate.main(["evaluate.py", "unused.db"])
    assert waits == [2.0, 2.0]


def test_pause_constant_default():
    assert evaluate.PAUSE_S == 2.0


def test_extra_trailing_column_passes_only_when_allowed():
    reference = [("a",), ("b",)]
    actual = [("a", 1), ("b", 2)]
    assert not compare_rows(reference, actual)
    assert not compare_rows(reference, actual, None, False)
    assert compare_rows(reference, actual, None, True)


def test_extra_columns_ok_still_fails_a_wrong_value():
    assert not compare_rows([("a",)], [("z", 1)], None, True)


def test_a_model_row_narrower_than_the_reference_fails():
    assert not compare_rows([("a", 1)], [("a",)], None, True)
    assert not compare_rows([("a", 1)], [("a",)])


def test_extra_columns_ok_works_with_ignore_columns():
    reference = [("highest", "a")]
    actual = [("most", "a", 99)]
    assert compare_rows(reference, actual, [0], True)


def test_history_is_passed_only_when_the_entry_has_one(monkeypatch, tmp_path):
    entries = [
        {"question": "plain", "reference_sql": "x"},
        {"question": "follow", "reference_sql": "x", "history": [{"question": "earlier"}]},
    ]
    (tmp_path / "known_answers.json").write_text(json.dumps(entries))
    monkeypatch.setattr(evaluate, "HERE", tmp_path)
    monkeypatch.setattr(evaluate, "PAUSE_S", 0)
    seen = []

    class Answer:
        error = None
        error_kind = None
        sql = ""
        elapsed_s = 0.0
        rows = [("highest", "a", 1.0)]

    def fake_ask(question, db, **kwargs):
        seen.append((question, kwargs))
        return Answer()

    monkeypatch.setattr(evaluate, "ask", fake_ask)
    monkeypatch.setattr(
        evaluate, "run_query", lambda sql, db: type("R", (), {"rows": [("highest", "a", 1.0)]})()
    )
    evaluate.main(["evaluate.py", "unused.db"])
    assert seen == [("plain", {}), ("follow", {"history": [{"question": "earlier"}]})]


def only_run(monkeypatch, tmp_path, argv_tail, questions):
    entries = [{"question": q, "reference_sql": "x"} for q in questions]
    (tmp_path / "known_answers.json").write_text(json.dumps(entries))
    monkeypatch.setattr(evaluate, "HERE", tmp_path)
    monkeypatch.setattr(evaluate, "PAUSE_S", 0)
    asked = []

    class Answer:
        error = None
        error_kind = None
        sql = ""
        elapsed_s = 0.0
        rows = [("a",)]

    def fake_ask(question, db, **kwargs):
        asked.append(question)
        return Answer()

    monkeypatch.setattr(evaluate, "ask", fake_ask)
    monkeypatch.setattr(evaluate, "run_query", lambda sql, db: type("R", (), {"rows": [("a",)]})())
    return asked, evaluate.main(["evaluate.py"] + argv_tail)


QUESTIONS = ["How many orders in 2016?", "Top 5 customers", "What is this database about?", "How many ORDERS in 2015?"]


def test_only_selects_entries_by_substring_ignoring_case(monkeypatch, tmp_path, capsys):
    asked, _ = only_run(monkeypatch, tmp_path, ["--only", "orders"], QUESTIONS)
    assert asked == ["How many orders in 2016?", "How many ORDERS in 2015?"]
    out = capsys.readouterr().out
    assert "--only selected 2 of 4 entries" in out
    assert "correct 2 of 2" in out


def test_several_only_values_add_up(monkeypatch, tmp_path, capsys):
    asked, _ = only_run(monkeypatch, tmp_path, ["--only", "top 5", "--only", "about"], QUESTIONS)
    assert asked == ["Top 5 customers", "What is this database about?"]
    assert "--only selected 2 of 4 entries" in capsys.readouterr().out


def test_an_unknown_only_value_selects_nothing_and_says_so(monkeypatch, tmp_path, capsys):
    asked, status = only_run(monkeypatch, tmp_path, ["--only", "zzz"], QUESTIONS)
    assert asked == [] and status == 0
    out = capsys.readouterr().out
    assert "--only selected 0 of 4 entries" in out
    assert "no entry matches the --only values" in out


def test_no_argument_runs_everything_with_the_old_summary(monkeypatch, tmp_path, capsys):
    asked, _ = only_run(monkeypatch, tmp_path, [], QUESTIONS)
    assert asked == QUESTIONS
    out = capsys.readouterr().out
    assert "--only" not in out
    assert "correct 4 of 4" in out


def test_only_still_accepts_a_database_path(monkeypatch, tmp_path):
    asked, _ = only_run(monkeypatch, tmp_path, ["other.db", "--only", "top"], QUESTIONS)
    assert asked == ["Top 5 customers"]


def test_only_without_a_value_is_an_error(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        only_run(monkeypatch, tmp_path, ["--only"], QUESTIONS)


def test_the_pause_counts_selected_entries_only(monkeypatch, tmp_path):
    waits = []
    monkeypatch.setattr(evaluate.time, "sleep", lambda s: waits.append(s))
    entries = [{"question": q, "reference_sql": "x"} for q in QUESTIONS]
    (tmp_path / "known_answers.json").write_text(json.dumps(entries))
    monkeypatch.setattr(evaluate, "HERE", tmp_path)
    monkeypatch.setattr(evaluate, "PAUSE_S", 2.0)

    class Answer:
        error = None
        error_kind = None
        sql = ""
        elapsed_s = 0.0
        rows = [("a",)]

    monkeypatch.setattr(evaluate, "ask", lambda q, db, **kw: Answer())
    monkeypatch.setattr(evaluate, "run_query", lambda sql, db: type("R", (), {"rows": [("a",)]})())
    evaluate.main(["evaluate.py", "--only", "orders"])
    assert waits == [2.0]


def alt_run(monkeypatch, tmp_path, entry, model_rows, run_query):
    (tmp_path / "known_answers.json").write_text(json.dumps([entry]))
    monkeypatch.setattr(evaluate, "HERE", tmp_path)
    monkeypatch.setattr(evaluate, "PAUSE_S", 0)

    class Answer:
        error = None
        error_kind = None
        sql = "SELECT model"
        elapsed_s = 0.0
        rows = model_rows

    monkeypatch.setattr(evaluate, "ask", lambda q, db, **kw: Answer())
    monkeypatch.setattr(evaluate, "run_query", run_query)
    return evaluate.main(["evaluate.py", "unused.db"])


def fake_run(table):
    def run(sql, db):
        return type("R", (), {"rows": table[sql]})()

    return run


def test_an_alternative_reference_can_make_a_fail_pass(monkeypatch, tmp_path, capsys):
    entry = {"question": "q", "reference_sql": "primary", "alt_reference_sqls": ["alt"]}
    run = fake_run({"primary": [("Margaret Peacock", 5)], "alt": [("Margaret", "Peacock", 5)]})
    alt_run(monkeypatch, tmp_path, entry, [("Margaret", "Peacock", 5)], run)
    out = capsys.readouterr().out
    assert "pass  0.00s  q" in out and "correct 1 of 1" in out


def test_the_primary_still_passes_when_an_alternative_exists(monkeypatch, tmp_path, capsys):
    entry = {"question": "q", "reference_sql": "primary", "alt_reference_sqls": ["alt"]}
    run = fake_run({"primary": [("Margaret Peacock", 5)], "alt": [("Margaret", "Peacock", 5)]})
    alt_run(monkeypatch, tmp_path, entry, [("Margaret Peacock", 5)], run)
    assert "correct 1 of 1" in capsys.readouterr().out


def test_no_alternative_leaves_the_fail(monkeypatch, tmp_path, capsys):
    entry = {"question": "q", "reference_sql": "primary"}
    run = fake_run({"primary": [("Margaret Peacock", 5)]})
    alt_run(monkeypatch, tmp_path, entry, [("Margaret", "Peacock", 5)], run)
    out = capsys.readouterr().out
    assert "fail  0.00s  q" in out and "alternatives tried" not in out


def test_a_fail_with_alternatives_says_how_many_were_tried(monkeypatch, tmp_path, capsys):
    entry = {"question": "q", "reference_sql": "primary", "alt_reference_sqls": ["a1", "a2"]}
    run = fake_run({"primary": [("x",)], "a1": [("y",)], "a2": [("z",)]})
    alt_run(monkeypatch, tmp_path, entry, [("nothing",)], run)
    out = capsys.readouterr().out
    assert "fail  0.00s  q" in out
    assert "expected rows: [('x',)]" in out
    assert "alternatives tried: 2" in out


def test_a_bad_alternative_is_reported_clearly_not_swallowed(monkeypatch, tmp_path):
    entry = {"question": "q", "reference_sql": "primary", "alt_reference_sqls": ["broken"]}

    def run(sql, db):
        if sql == "broken":
            raise ValueError("syntax error near secret text")
        return type("R", (), {"rows": [("x",)]})()

    with pytest.raises(RuntimeError) as caught:
        alt_run(monkeypatch, tmp_path, entry, [("x",)], run)
    assert "alternative reference 1 for 'q' could not be run: ValueError" in str(caught.value)
    assert "secret text" not in str(caught.value)


def test_an_alternative_uses_the_entry_comparison_settings(monkeypatch, tmp_path, capsys):
    entry = {"question": "q", "reference_sql": "primary", "alt_reference_sqls": ["alt"], "extra_columns_ok": True}
    run = fake_run({"primary": [("zzz",)], "alt": [("a",)]})
    alt_run(monkeypatch, tmp_path, entry, [("a", 99)], run)
    assert "correct 1 of 1" in capsys.readouterr().out
