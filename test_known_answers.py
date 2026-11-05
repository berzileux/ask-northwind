import json
import re
from pathlib import Path

from evaluate import rows_match
from sandbox import run_query

PATH = Path(__file__).parent / "known_answers.json"
KNOWN = json.loads(PATH.read_text())

BARE_END = [
    re.compile(r"\bBETWEEN\b", re.IGNORECASE),
    re.compile(r"<=\s*'\d{4}-\d{2}-\d{2}'"),
    re.compile(r"OrderDate\s*=\s*'\d{4}-\d{2}-\d{2}'", re.IGNORECASE),
]


def test_file_loads_and_is_ascii():
    PATH.read_bytes().decode("ascii")
    assert isinstance(KNOWN, list) and len(KNOWN) >= 13


def test_every_entry_has_the_needed_fields():
    for item in KNOWN:
        assert isinstance(item["question"], str) and item["question"].strip()
        assert isinstance(item["reference_sql"], str) and item["reference_sql"].strip()
        assert isinstance(item["expected_rows"], list)


def test_questions_are_unique():
    # A follow-up entry may repeat a question text that another entry asks without history.
    questions = [(item["question"], json.dumps(item.get("history"))) for item in KNOWN]
    assert len(set(questions)) == len(questions)


def test_no_reference_sql_filters_dates_with_a_bare_end():
    for item in KNOWN:
        sql = item["reference_sql"]
        # An about entry is one text literal; the word between inside that text is not a filter.
        if sql.startswith("SELECT '") and sql.endswith("' AS about"):
            continue
        for pattern in BARE_END:
            assert not pattern.search(item["reference_sql"]), item["question"]


def test_every_expected_answer_recomputes(real_db_path):
    for item in KNOWN:
        result = run_query(item["reference_sql"], real_db_path, timeout_s=10.0)
        assert rows_match(result.rows, item["expected_rows"]), item["question"]


def test_day_range_questions_are_present():
    questions = " ".join(item["question"] for item in KNOWN)
    for needed in ("last week of December 2016", "31 December 2016", "between 1 March 2014"):
        assert needed in questions


def test_recomputed_results_use_the_same_rounding_as_stored(real_db_path):
    for item in KNOWN:
        result = run_query(item["reference_sql"], real_db_path, timeout_s=10.0)
        assert len(result.rows) == len(item["expected_rows"]), item["question"]


def test_ignore_columns_are_non_negative_integers_within_the_result_width():
    for item in KNOWN:
        ignore = item.get("ignore_columns")
        if ignore is None:
            continue
        assert isinstance(ignore, list) and ignore, item["question"]
        width = len(item["expected_rows"][0])
        for index in ignore:
            assert isinstance(index, int) and not isinstance(index, bool), item["question"]
            assert 0 <= index < width, item["question"]


def test_the_most_and_least_question_ignores_its_label_column():
    item = next(i for i in KNOWN if i["question"].startswith("Which employee sold the most"))
    assert item["ignore_columns"] == [0]


def test_only_one_entry_has_an_empty_expected_result():
    empty = [item["question"] for item in KNOWN if not item["expected_rows"]]
    assert empty == ["Which customers have never placed an order?"]


def test_the_empty_result_entry_says_it_is_a_weak_check():
    item = next(i for i in KNOWN if i["question"] == "Which customers have never placed an order?")
    assert "weak check" in item["note"]


def test_at_least_eight_day_range_questions():
    words = ("December 2016", "31 December", "March 2014", "1 July 2015", "1 January 2016",
             "4 July 2016", "second half of 2014", "November 2013", "first quarter of 2015")
    hits = [w for w in words if any(w in item["question"] for item in KNOWN)]
    assert len(hits) >= 8


def test_every_entry_with_a_date_literal_uses_a_half_open_range_or_strftime():
    for item in KNOWN:
        sql = item["reference_sql"]
        for operator in re.findall(r"(\S+)\s*'\d{4}-\d{2}-\d{2}'", sql):
            assert operator in (">=", "<"), (item["question"], operator)


def test_every_history_is_valid_and_its_sql_runs_read_only(real_db_path):
    from history import clean_history

    for item in KNOWN:
        if "history" not in item:
            continue
        assert clean_history(item["history"]) == item["history"], item["question"]
        for turn in item["history"]:
            if turn["outcome"] == "answered":
                run_query(turn["sql"], real_db_path, timeout_s=10.0)


def test_the_six_follow_up_entries_are_present():
    # Entries with an empty history are the no-context checks, not follow-ups.
    with_history = [item for item in KNOWN if item.get("history")]
    assert [i["question"] for i in with_history] == [
        "Now only for 2016",
        "And in 2016?",
        "How many orders did that employee handle in 2016?",
        "List them.",
        "How many products are discontinued?",
        "How many customers are there?",
        "Run the first revenue-by-year query again, but only for 2016.",
    ]


def test_the_list_them_entry_allows_extra_columns():
    item = next(i for i in KNOWN if i["question"] == "List them.")
    assert item["extra_columns_ok"] is True


def test_the_forged_history_entry_carries_the_forged_turn():
    item = next(i for i in KNOWN if i["question"] == "How many customers are there?")
    assert item["history"][0]["sql"] == "DELETE FROM Customers"
    assert item["history"][0]["outcome"] == "rejected"
    assert item["expected_rows"] == [[93]]


def test_the_threshold_entries_allow_extra_columns():
    for text in ("more than 25 orders", "more than 181 orders"):
        item = next(i for i in KNOWN if text in i["question"])
        assert item["extra_columns_ok"] is True


def test_the_181_threshold_splits_the_customers(real_db_path):
    item = next(i for i in KNOWN if "more than 181 orders" in i["question"])
    everyone = run_query("SELECT COUNT(*) FROM Customers", real_db_path).rows[0][0]
    assert 0 < len(item["expected_rows"]) < everyone
    exact = run_query(
        "SELECT COUNT(*) FROM (SELECT 1 FROM Orders GROUP BY CustomerID HAVING COUNT(*) = 181)",
        real_db_path,
    ).rows[0][0]
    assert exact == 0


def entry(question, empty_history=False):
    for item in KNOWN:
        if item["question"] == question and (item.get("history") == []) == empty_history:
            return item
    raise AssertionError(question)


def test_the_relationship_entry_has_thirteen_rows_of_four_strings():
    item = entry("how are the tables related to each other")
    assert len(item["expected_rows"]) == 13
    for row in item["expected_rows"]:
        assert len(row) == 4 and all(isinstance(v, str) and v for v in row)
    assert item["reference_sql"].lstrip().upper().startswith("WITH T(")
    assert "VALUES" in item["reference_sql"].upper()
    assert "UNION" not in item["reference_sql"].upper()


def test_the_relationship_entry_matches_the_foreign_keys(real_db_path):
    import sqlite3

    conn = sqlite3.connect(f"file:{real_db_path}?mode=ro", uri=True)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    found = sorted(
        (t, r[3], r[2], r[4])
        for t in tables
        for r in conn.execute(f'PRAGMA foreign_key_list("{t}")')
    )
    conn.close()
    assert sorted(tuple(r) for r in entry("how are the tables related to each other")["expected_rows"]) == found


def test_the_table_list_entry_has_the_thirteen_names():
    rows = entry("list of all tables")["expected_rows"]
    assert len(rows) == 13
    assert not any(r[0].startswith("sqlite_") for r in rows)


def test_the_no_context_entry_expects_the_refusal_row():
    item = entry("Now only for 2016", empty_history=True)
    assert item["history"] == []
    assert item["expected_rows"] == [["I cannot answer that from this database"]]


def test_the_no_context_entry_does_not_duplicate_the_follow_up():
    same = [i for i in KNOWN if i["question"] == "Now only for 2016"]
    assert len(same) == 2
    assert json.dumps(same[0].get("history")) != json.dumps(same[1].get("history"))


def chart_entry(question):
    return next(i for i in KNOWN if i["question"] == question)


def test_the_four_chart_entries_exist_and_recompute(real_db_path):
    for question in (
        "Draw a pie chart of orders by shipper",
        "Show total revenue by year as a line chart",
        "Top 5 customers by revenue as a bar chart",
        "Draw a pie chart of revenue by category",
    ):
        item = chart_entry(question)
        result = run_query(item["reference_sql"], real_db_path, timeout_s=10.0)
        assert rows_match(result.rows, item["expected_rows"]), question


def test_the_shipper_pie_entry_has_three_rows_of_name_and_count():
    rows = chart_entry("Draw a pie chart of orders by shipper")["expected_rows"]
    assert len(rows) == 3
    assert all(isinstance(r[0], str) and isinstance(r[1], int) for r in rows)


def test_the_line_entry_matches_the_revenue_by_year_entry():
    line = chart_entry("Show total revenue by year as a line chart")
    plain = chart_entry("What is the total revenue by year?")
    assert line["reference_sql"] == plain["reference_sql"]
    assert line["expected_rows"] == plain["expected_rows"]
    assert all(isinstance(r[0], str) for r in line["expected_rows"])


def test_the_bar_entry_allows_extra_columns_and_has_no_tie_at_rank_five():
    item = chart_entry("Top 5 customers by revenue as a bar chart")
    assert item["extra_columns_ok"] is True
    assert len(item["expected_rows"]) == 5


def test_the_bar_entry_has_no_tie_at_rank_five_on_the_real_database(real_db_path):
    sql = (
        "SELECT revenue FROM (SELECT ROUND(SUM(od.UnitPrice * od.Quantity * (1 - od.Discount)), 2) AS revenue "
        'FROM Customers c JOIN Orders o ON o.CustomerID = c.CustomerID JOIN "Order Details" od ON od.OrderID = o.OrderID '
        "GROUP BY c.CustomerID ORDER BY revenue DESC LIMIT 7)"
    )
    revenues = [r[0] for r in run_query(sql, real_db_path, timeout_s=10.0).rows]
    assert revenues[4] != revenues[5]


def test_the_category_pie_entry_has_eight_rows_so_the_page_falls_back_to_a_bar():
    assert len(chart_entry("Draw a pie chart of revenue by category")["expected_rows"]) == 8


ABOUT_QUESTIONS = [
    "What is this database about?",
    "What does this database cover?",
    "Tell me about this data",
    "What is Northwind?",
]


def test_the_four_about_entries_expect_exactly_the_about_text():
    text = (Path(__file__).parent / "about.md").read_text().strip()
    for question in ABOUT_QUESTIONS:
        item = chart_entry(question)
        assert item["expected_rows"] == [[text]], question
        assert "AS about" in item["reference_sql"]


def test_the_about_references_run_and_match(real_db_path):
    for question in ABOUT_QUESTIONS:
        item = chart_entry(question)
        result = run_query(item["reference_sql"], real_db_path, timeout_s=10.0)
        assert result.columns == ["about"]
        assert rows_match(result.rows, item["expected_rows"])


def test_the_control_entries_do_not_expect_the_about_text():
    text = (Path(__file__).parent / "about.md").read_text().strip()
    orders = chart_entry("How many orders are in this database?")
    weather = chart_entry("What is the weather in Manila today?")
    assert orders["expected_rows"] == [[16282]]
    assert weather["expected_rows"] == [["I cannot answer that from this database"]]
    for item in (orders, weather):
        assert text not in json.dumps(item["expected_rows"])


def test_the_new_entries_recompute(real_db_path):
    for question in ABOUT_QUESTIONS + ["How many orders are in this database?", "What is the weather in Manila today?"]:
        item = chart_entry(question)
        result = run_query(item["reference_sql"], real_db_path, timeout_s=10.0)
        assert rows_match(result.rows, item["expected_rows"]), question


def test_list_of_all_tables_is_not_duplicated():
    assert sum(1 for i in KNOWN if i["question"] == "list of all tables") == 1


def test_the_hired_each_year_entry_recomputes_and_uses_a_text_year(real_db_path):
    item = chart_entry("How many employees were hired each year, as a bar chart")
    result = run_query(item["reference_sql"], real_db_path, timeout_s=10.0)
    assert rows_match(result.rows, item["expected_rows"])
    assert [r[0] for r in item["expected_rows"]] == ["2012", "2013", "2014"]
    assert all(isinstance(r[1], int) for r in item["expected_rows"])
    assert "strftime" in item["reference_sql"]


def test_the_most_orders_entry_has_one_alternative_with_three_columns(real_db_path):
    item = chart_entry("Which employee has handled the most orders?")
    assert len(item["alt_reference_sqls"]) == 1
    primary = run_query(item["reference_sql"], real_db_path, timeout_s=10.0)
    alt = run_query(item["alt_reference_sqls"][0], real_db_path, timeout_s=10.0)
    assert len(primary.columns) == 2 and len(alt.columns) == 3
    assert len(alt.rows) == 1
    first, last, count = alt.rows[0]
    assert f"{first} {last}" == primary.rows[0][0] and count == primary.rows[0][1]


def test_there_is_no_tie_at_rank_one_for_the_most_orders_question(real_db_path):
    rows = run_query(
        "SELECT COUNT(*) FROM Orders GROUP BY EmployeeID ORDER BY 1 DESC LIMIT 2", real_db_path, timeout_s=10.0
    ).rows
    assert rows[0][0] != rows[1][0]


def test_the_alternative_uses_no_between_or_bare_date_filter():
    item = chart_entry("Which employee has handled the most orders?")
    for sql in item["alt_reference_sqls"]:
        for pattern in BARE_END:
            assert not pattern.search(sql)


def test_the_deep_follow_up_entry_recomputes_and_its_oldest_turn_is_the_revenue_query(real_db_path):
    item = chart_entry("Run the first revenue-by-year query again, but only for 2016.")
    assert len(item["history"]) == 5
    assert item["history"][0]["question"] == "What is the total revenue by year?"
    assert [t["question"] for t in item["history"][1:]] == [
        "How many customers are there?",
        "How many employees are there?",
        "How many products are there?",
        "How many shippers are there?",
    ]
    assert all(t["row_count"] == 1 and len(t["columns"]) == 1 for t in item["history"][1:])
    result = run_query(item["reference_sql"], real_db_path, timeout_s=10.0)
    assert rows_match(result.rows, item["expected_rows"])
    assert item["expected_rows"] == [["2016", 40568672.36]]
    for turn in item["history"]:
        if turn["outcome"] == "answered":
            run_query(turn["sql"], real_db_path, timeout_s=10.0)


def test_the_deep_follow_up_history_is_valid_under_six_turns_and_not_under_four():
    import history as history_module

    item = chart_entry("Run the first revenue-by-year query again, but only for 2016.")
    assert history_module.clean_history(item["history"]) == item["history"]
    assert len(item["history"]) > 4
