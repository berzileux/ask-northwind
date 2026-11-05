import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed; the chart logic tests run it")

SOURCE = (Path(__file__).parent / "index.html").read_text()
BEGIN = "/* CHART-LOGIC BEGIN */"
END = "/* CHART-LOGIC END */"

RUNNER = """
const fs = require('fs');
const vm = require('vm');
const ctx = {};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const exprs = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
console.log(JSON.stringify(exprs.map(e => vm.runInContext(e, ctx))));
"""


def block():
    start = SOURCE.index(BEGIN) + len(BEGIN)
    return SOURCE[start:SOURCE.index(END)]


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    folder = tmp_path_factory.mktemp("charts")
    (folder / "logic.js").write_text(block())
    (folder / "runner.js").write_text(RUNNER)

    def evaluate(*expressions):
        (folder / "exprs.json").write_text(json.dumps(list(expressions)))
        done = subprocess.run(
            [NODE, str(folder / "runner.js"), str(folder / "logic.js"), str(folder / "exprs.json")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)

    return evaluate


def test_markers_exist_once_in_order():
    assert SOURCE.count(BEGIN) == 1 and SOURCE.count(END) == 1
    assert SOURCE.index(BEGIN) < SOURCE.index(END)


def test_block_uses_no_document_or_window():
    text = block()
    assert not re.search(r"\b(document|window|navigator|localStorage|fetch)\b", text)


def test_block_defines_the_six_functions():
    text = block()
    for name in ("fmtShort", "niceMax", "chartData", "availability", "wanted", "pickView"):
        assert f"function {name}(" in text


def test_fmt_short(run):
    values = run(
        "fmtShort(12)", "fmtShort(0)", "fmtShort(502000)", "fmtShort(38600000)", "fmtShort(1200000)",
        "fmtShort(1234)", "fmtShort(1000)", "fmtShort(12.345)", "fmtShort(2000000)",
    )
    assert values == ["12", "0", "502K", "38.6M", "1.2M", "1.2K", "1K", "12.35", "2M"]


def test_nice_max(run):
    values = run(
        "niceMax(0)", "niceMax(-3)", "niceMax(1)", "niceMax(1.5)", "niceMax(2)", "niceMax(2.2)",
        "niceMax(4.1)", "niceMax(5)", "niceMax(7)", "niceMax(77310562.8)", "niceMax(6015)",
    )
    assert values == [1, 1, 1, 2, 2, 2.5, 5, 5, 10, 100000000, 10000]


GOOD = "chartData(['name','n'], [['a',1],['b',2],['c',3]])"


def test_chart_data_good_result(run):
    (out,) = run(GOOD)
    assert out == {
        "labels": ["a", "b", "c"],
        "values": [1, 2, 3],
        "labelIndex": 0,
        "valueIndex": 1,
        "labelNames": ["name"],
        "valueName": "n",
    }


def test_chart_data_label_may_come_second(run):
    (out,) = run("chartData(['n','name'], [[1,'a'],[2,'b']])")
    assert out["labelIndex"] == 1 and out["valueIndex"] == 0
    assert out["labels"] == ["a", "b"] and out["values"] == [1, 2]


@pytest.mark.parametrize(
    "call, reason",
    [
        ("chartData(['a','n'], [['x',1]])", "One row: the number is the answer, nothing to chart."),
        ("chartData(['a','n'], [])", "One row: the number is the answer, nothing to chart."),
        ("chartData(['n'], [[1],[2]])", "Charts need at least one label column."),
        ("chartData(['a','n'], [['x',1],['y',-2]])", "Charts need non-negative numbers."),
        ("chartData(['a','n'], [['x',1],['y',null]])", "Charts need every value present."),
        ("chartData(['a','n'], [['x','1'],['y','2']])", "Charts need a number column to measure."),
        ("chartData(['a','n'], [['x',1],['y','9007199254740993']])", "Charts need a number column to measure."),
    ],
)
def test_chart_data_reasons(run, call, reason):
    assert run(call) == [reason]


def test_chart_data_a_null_label_is_none(run):
    (out,) = run("chartData(['a','n'], [[null,1],['y',2]])")
    assert out["labels"] == ["(none)", "y"]


def rows(n, label="'r'+i", value="i+1"):
    return f"Array.from({{length:{n}}}, (_, i) => [{label}, {value}])"


@pytest.mark.parametrize("n, bar, pie", [(1, False, False), (2, True, False), (3, True, True), (6, True, True), (7, True, False), (30, True, False), (31, False, False)])
def test_availability_row_limits(run, n, bar, pie):
    (av,) = run(f"availability(['a','n'], {rows(n)})")
    if n < 2:
        assert av["bar"] == av["pie"] == av["line"] == "One row: the number is the answer, nothing to chart."
        return
    assert (av["bar"] == "") is bar
    assert (av["pie"] == "") is pie
    if not bar:
        assert av["bar"] == f"Bar needs 2 to 30 rows; this has {n}."
    if not pie:
        assert av["pie"] == f"Pie needs 3 to 6 parts, all above zero; this has {n}."


def test_pie_needs_every_value_above_zero(run):
    (av,) = run("availability(['a','n'], [['x',1],['y',0],['z',2]])")
    assert av["pie"] == "Pie needs 3 to 6 parts, all above zero; this has 3."
    assert av["bar"] == ""


@pytest.mark.parametrize("n, line", [(2, False), (3, True), (60, True), (61, False)])
def test_availability_line_point_limits(run, n, line):
    (av,) = run(f"availability(['d','n'], {rows(n, label='String(2000 + (i % 99))')})")
    assert (av["line"] == "") is line
    if not line:
        assert av["line"] == "Line needs 3 or more points ordered by date, year or month."


@pytest.mark.parametrize(
    "label, ok",
    [("2016", True), ("2016-07", True), ("2016-07-04", True), ("2016/07", True), ("2016/07/04", True), ("Q1", False), ("16", False), ("2016-7", False), ("2016-07-04 10:00", False), ("x2016", False)],
)
def test_line_label_pattern(run, label, ok):
    (av,) = run(f"availability(['d','n'], [['{label}',1],['{label}',2],['{label}',3]])")
    assert (av["line"] == "") is ok


def test_availability_passes_the_reason_to_all_three(run):
    (av,) = run("availability(['a','n'], [['x',-1],['y',2]])")
    assert av["bar"] == av["pie"] == av["line"] == "Charts need non-negative numbers."
    assert av["data"] is None


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Draw a pie chart of orders by shipper", "pie"),
        ("PIE", "pie"),
        ("Show total revenue by year as a line chart", "line"),
        ("a line graph please", "line"),
        ("what is the sales trend", "line"),
        ("Top 5 customers by revenue as a bar chart", "bar"),
        ("show me a graph", "bar"),
        ("plot the orders", "bar"),
        ("a chart of sales", "bar"),
        ("which employee sold the most", ""),
        ("bargain products", ""),
        ("pies and pipeline", ""),
        ("outline the trends", ""),
        ("a pie line chart", "pie"),
        ("line chart bar", "line"),
        ("", ""),
    ],
)
def test_wanted(run, question, expected):
    assert run(f"wanted({json.dumps(question)})") == [expected]


def test_wanted_handles_missing_text(run):
    assert run("wanted(null)", "wanted(undefined)") == ["", ""]


ALL = {"bar": "", "pie": "", "line": ""}
NONE_BAR = {"bar": "", "pie": "Pie needs 3 to 6 parts, all above zero; this has 8.", "line": "Line needs 3 or more points ordered by date, year or month."}
REASON = {"bar": "r", "pie": "r", "line": "r"}


def pick(wanted, availability):
    return f"pickView({json.dumps(wanted)}, {json.dumps(availability)})"


def test_pick_view_branches(run):
    out = run(
        pick("", ALL),
        pick("pie", ALL),
        pick("line", ALL),
        pick("bar", ALL),
        pick("pie", NONE_BAR),
        pick("line", NONE_BAR),
        pick("bar", REASON),
        pick("pie", REASON),
        pick("line", {"bar": "no bar", "pie": "", "line": "no line"}),
    )
    assert out[0] == {"view": "table", "note": ""}
    assert out[1] == {"view": "pie", "note": "Showing a pie chart because your question asked for one."}
    assert out[2] == {"view": "line", "note": "Showing a line chart because your question asked for one."}
    assert out[3] == {"view": "bar", "note": "Showing a bar chart because your question asked for one."}
    assert out[4] == {
        "view": "bar",
        "note": "Pie needs 3 to 6 parts, all above zero; this has 8. Showing a bar chart instead.",
    }
    assert out[5] == {
        "view": "bar",
        "note": "Line needs 3 or more points ordered by date, year or month. Showing a bar chart instead.",
    }
    assert out[6] == {"view": "table", "note": "r Showing the table instead."}
    assert out[7] == {"view": "table", "note": "r Showing the table instead."}
    assert out[8] == {"view": "table", "note": "no line Showing the table instead."}


NAMES = "[['Margaret','Peacock',1908],['Nancy','Davolio',1000],['Janet','Leverling',900]]"


def test_first_last_count_gives_a_bar_with_the_joined_label(run):
    (av,) = run(f"availability(['FirstName','LastName','OrderCount'], {NAMES})")
    assert av["bar"] == ""
    assert av["data"]["labels"][0] == "Margaret Peacock"
    assert av["data"]["labelNames"] == ["FirstName", "LastName"]
    assert av["data"]["valueName"] == "OrderCount"
    assert av["line"].startswith("Line needs 3 or more points")


EXAMPLE = "How many employees were hired each year?"


def test_first_last_hire_year_as_integers_gives_no_chart_and_the_example(run):
    (av,) = run("availability(['FirstName','LastName','HireYear'], [['A','B',2012],['C','D',2013],['E','F',2014]])")
    expected = f"Charts need a number column to measure. HireYear is treated as a label. Ask for a count or total, for example: {EXAMPLE}"
    assert av["bar"] == av["pie"] == av["line"] == expected
    assert av["data"] is None


def test_full_name_and_hire_year_as_text_gives_the_same_reason(run):
    (av,) = run("availability(['FullName','HireYear'], [['Nancy Davolio','2012'],['Janet Leverling','2013'],['Margaret Peacock','2014']])")
    expected = f"Charts need a number column to measure. HireYear is treated as a label. Ask for a count or total, for example: {EXAMPLE}"
    assert av["bar"] == av["pie"] == av["line"] == expected


def test_full_name_hire_year_and_a_count_allow_bar_but_not_line(run):
    (av,) = run("availability(['FullName','HireYear','Count'], [['Nancy Davolio','2012',5],['Janet Leverling','2013',6],['Margaret Peacock','2014',7]])")
    assert av["data"]["labels"][0] == "Nancy Davolio 2012"
    assert av["bar"] == ""
    assert av["line"] == "Line needs 3 or more points ordered by date, year or month."


def test_order_year_as_text_with_revenue_allows_bar_and_line(run):
    (av,) = run("availability(['OrderYear','Revenue'], [['2012',10.5],['2013',20.5],['2014',30.5]])")
    assert av["bar"] == "" and av["line"] == ""
    assert av["data"]["labels"] == ["2012", "2013", "2014"]
    assert av["data"]["valueName"] == "Revenue"


def test_order_year_as_integer_with_revenue_allows_bar_and_line(run):
    (av,) = run("availability(['OrderYear','Revenue'], [[2012,10.5],[2013,20.5],[2014,30.5]])")
    assert av["bar"] == "" and av["line"] == ""
    assert av["data"]["labels"] == ["2012", "2013", "2014"]
    assert av["data"]["valueIndex"] == 1


@pytest.mark.parametrize("values", ["'99'", "'abcd'", "'12345'", "'0999'", "'3000'"])
def test_a_year_header_with_text_that_is_not_a_year_is_not_a_year_column(run, values):
    (out,) = run(f"chartData(['Year','n'], [[{values},1],[{values},2]])")
    assert out["labelNames"] == ["Year"] and out["valueName"] == "n"


@pytest.mark.parametrize("a, b", [("5", "6"), ("99", "98"), ("3000", "3001"), ("2012.5", "2013.5"), ("999", "1000.5")])
def test_a_year_header_with_values_outside_the_year_rule_stays_a_number_column(run, a, b):
    (out,) = run(f"chartData(['k','Year'], [['x',{a}],['y',{b}]])")
    assert out["valueName"] == "Year" and out["labelNames"] == ["k"]


def test_years_header_with_small_numbers_stays_a_number_column(run):
    (out,) = run("chartData(['name','Years'], [['a',5],['b',6],['c',7]])")
    assert out["valueName"] == "Years" and out["values"] == [5, 6, 7]


def test_a_year_column_with_one_bad_value_is_not_a_year_column(run):
    (out,) = run("chartData(['name','Year'], [['a',2012],['b',99],['c',2014]])")
    assert out["valueName"] == "Year"


def test_a_year_column_with_a_null_is_not_a_year_column(run):
    assert run("chartData(['name','Year'], [['a',2012],['b',null],['c',2014]])") == ["Charts need every value present."]


def test_the_header_rule_is_contains_year_so_bearing_year_counts(run):
    # Accepted: the header rule is "contains year", so BearingYear is a label when its values look like years.
    (out,) = run("chartData(['name','BearingYear','n'], [['a',2001,1],['b',2002,2]])")
    assert out["labelNames"] == ["name", "BearingYear"] and out["valueName"] == "n"


@pytest.mark.parametrize("header", ["year", "YEAR", "HireYear", "order_year", "yearly"])
def test_the_year_word_is_matched_in_any_case_and_position(run, header):
    (out,) = run(f"chartData(['name','{header}'], [['a',2012],['b',2013]])")
    assert isinstance(out, str) and "treated as a label" in out


def test_four_label_columns_are_refused(run):
    (out,) = run("chartData(['a','b','c','d','n'], [['1','2','3','4',1],['5','6','7','8',2]])")
    assert out == "Charts need at most three label columns."


def test_three_label_columns_are_allowed(run):
    (out,) = run("chartData(['a','b','c','n'], [['1','2','3',1],['5','6','7',2]])")
    assert out["labels"] == ["1 2 3", "5 6 7"] and out["labelNames"] == ["a", "b", "c"]


def test_two_numeric_non_year_columns_are_refused(run):
    (out,) = run("chartData(['a','n','m'], [['x',1,2],['y',3,4]])")
    assert out == "Charts need exactly one number column."


def test_no_number_and_no_year_gives_the_plain_reason(run):
    (out,) = run("chartData(['a','b'], [['x','y'],['p','q']])")
    assert out == "Charts need a number column to measure."


def test_a_null_label_part_is_skipped_and_all_empty_gives_none(run):
    (out,) = run("chartData(['first','last','n'], [['Ann',null,1],[null,null,2],['', '',3],[null,'Lee',4]])")
    assert out["labels"] == ["Ann", "(none)", "(none)", "Lee"]


def test_the_line_pattern_refuses_a_joined_label_and_accepts_a_lone_year(run):
    (joined,) = run("availability(['name','year','n'], [['Margaret','2012',1],['Nancy','2013',2],['Janet','2014',3]])")
    assert joined["data"]["labels"][0] == "Margaret 2012" and joined["line"] != ""
    (lone,) = run("availability(['year','n'], [['2012',1],['2013',2],['2014',3]])")
    assert lone["line"] == ""


def test_a_year_label_with_a_zero_or_negative_measure_keeps_the_old_rules(run):
    assert run("chartData(['OrderYear','n'], [['2012',-1],['2013',2]])") == ["Charts need non-negative numbers."]
    (av,) = run("availability(['OrderYear','n'], [['2012',0],['2013',2]])")
    assert av["pie"].startswith("Pie needs 3 to 6 parts")


def test_the_example_question_text_is_fixed(run):
    (out,) = run("chartData(['EmployeeYear'], [[2012],[2013]])")
    assert out.endswith("Ask for a count or total, for example: How many employees were hired each year?")


def test_two_parts_are_not_a_pie_and_three_are(run):
    (two,) = run("availability(['a','n'], [['x',1],['y',2]])")
    (three,) = run("availability(['a','n'], [['x',1],['y',2],['z',3]])")
    assert two["pie"] == "Pie needs 3 to 6 parts, all above zero; this has 2."
    assert two["bar"] == ""
    assert three["pie"] == ""


def test_a_two_part_pie_question_falls_back_to_a_bar(run):
    (pick,) = run("pickView('pie', availability(['a','n'], [['x',1],['y',2]]))")
    assert pick == {"view": "bar", "note": "Pie needs 3 to 6 parts, all above zero; this has 2. Showing a bar chart instead."}


STAFF = (
    "[['Andrew Fuller','2012'],['Janet Leverling','2012'],['Nancy Davolio','2012'],"
    "['Margaret Peacock','2013'],['Michael Suyama','2013'],['Steven Buchanan','2013'],"
    "['Anne Dodsworth','2014'],['Laura Callahan','2014'],['Robert King','2014']]"
)


def groups(run, columns, rows):
    (out,) = run(f"groupsData({columns}, {rows})")
    return out


def test_fullname_and_text_hire_year_gives_three_groups_in_first_appearance_order(run):
    out = groups(run, "['fullname','hire_year']", STAFF)
    assert out["ok"] is True
    assert out["groupName"] == "hire_year" and out["itemNames"] == ["fullname"]
    assert out["rowCount"] == 9
    assert [g["label"] for g in out["groups"]] == ["2012", "2013", "2014"]
    assert out["groups"][0]["items"] == ["Andrew Fuller", "Janet Leverling", "Nancy Davolio"]
    assert out["groups"][2]["items"] == ["Anne Dodsworth", "Laura Callahan", "Robert King"]


def test_first_last_hire_year_joins_the_item_under_the_year_group(run):
    rows = "[['Margaret','Peacock','2013'],['Nancy','Davolio','2012'],['Janet','Leverling','2012'],['Steven','Buchanan','2013']]"
    out = groups(run, "['FirstName','LastName','HireYear']", rows)
    assert out["ok"] and out["groupName"] == "HireYear" and out["itemNames"] == ["FirstName", "LastName"]
    assert [g["label"] for g in out["groups"]] == ["2013", "2012"]
    assert out["groups"][0]["items"] == ["Margaret Peacock", "Steven Buchanan"]


def test_an_integer_year_column_behaves_the_same(run):
    rows = "[['Andrew Fuller',2012],['Janet Leverling',2012],['Margaret Peacock',2013],['Steven Buchanan',2013]]"
    out = groups(run, "['fullname','hire_year']", rows)
    assert out["ok"] and out["groupName"] == "hire_year"
    assert [g["label"] for g in out["groups"]] == ["2012", "2013"]


def test_without_a_year_the_first_label_column_is_the_group_and_the_last_the_item(run):
    rows = "[['Beverages','Chai'],['Beverages','Chang'],['Condiments','Aniseed Syrup'],['Seafood','Ikura']]"
    out = groups(run, "['category','product']", rows)
    assert out["ok"] and out["groupName"] == "category" and out["itemNames"] == ["product"]
    assert [g["label"] for g in out["groups"]] == ["Beverages", "Condiments", "Seafood"]
    assert out["groups"][0]["items"] == ["Chai", "Chang"]


def test_with_three_text_columns_and_no_year_the_first_is_the_group_and_the_others_join(run):
    rows = "[['A','x','y'],['A','p','q'],['B','m','n']]"
    out = groups(run, "['g','i1','i2']", rows)
    assert out["ok"] and out["groupName"] == "g" and out["itemNames"] == ["i1", "i2"]
    assert out["groups"][0]["items"] == ["x y", "p q"]


def test_a_null_or_empty_group_becomes_none_and_a_none_group_keeps_order(run):
    rows = "[['g1','a'],[null,'b'],['','c'],['g1','d'],['g2','e']]"
    out = groups(run, "['grp','name']", rows)
    assert [g["label"] for g in out["groups"]] == ["g1", "(none)", "g2"]
    assert out["groups"][1]["items"] == ["b", "c"]


def test_null_and_empty_item_parts_are_skipped_and_an_all_empty_item_is_none(run):
    rows = "[['g1','Ann',null],['g1',null,null],['g2','',''],['g2',null,'Lee']]"
    out = groups(run, "['grp','first','last']", rows)
    assert out["groups"][0]["items"] == ["Ann", "(none)"]
    assert out["groups"][1]["items"] == ["(none)", "Lee"]


def test_the_sql_order_is_kept_with_no_sorting(run):
    rows = "[['z','2014'],['a','2012'],['m','2014'],['b','2012']]"
    out = groups(run, "['name','hire_year']", rows)
    assert [g["label"] for g in out["groups"]] == ["2014", "2012"]
    assert out["groups"][0]["items"] == ["z", "m"]


def test_one_row_and_sixty_one_rows_are_refused_with_the_count_and_sixty_is_accepted(run):
    def rows(n):
        return f"Array.from({{length:{n}}}, (_, i) => ['g' + (i % 3), 'item' + i])"

    one = groups(run, "['grp','name']", rows(1))
    assert one == {"ok": False, "reason": "Groups needs 2 to 60 rows; this has 1."}
    many = groups(run, "['grp','name']", rows(61))
    assert many == {"ok": False, "reason": "Groups needs 2 to 60 rows; this has 61."}
    assert groups(run, "['grp','name']", rows(60))["ok"] is True
    assert groups(run, "['grp','name']", rows(2))["ok"] is True
    assert groups(run, "['grp','name']", "[]")["reason"] == "Groups needs 2 to 60 rows; this has 0."


def test_one_group_and_twenty_one_groups_are_refused_with_the_count(run):
    one = groups(run, "['grp','name']", "[['g','a'],['g','b'],['g','c']]")
    assert one == {"ok": False, "reason": "Groups needs 2 to 20 groups; this has 1."}
    many = groups(run, "['grp','name']", "Array.from({length:21}, (_, i) => ['g' + i, 'n' + i])")
    assert many == {"ok": False, "reason": "Groups needs 2 to 20 groups; this has 21."}
    twenty = groups(run, "['grp','name']", "Array.from({length:20}, (_, i) => ['g' + i, 'n' + i])")
    assert twenty["ok"] is True and len(twenty["groups"]) == 20


def test_one_label_column_only_is_refused(run):
    out = groups(run, "['name']", "[['a'],['b']]")
    assert out == {"ok": False, "reason": "Groups needs a group column and an item column."}


def test_four_label_columns_are_refused(run):
    out = groups(run, "['a','b','c','d']", "[['1','2','3','4'],['5','6','7','8']]")
    assert out == {"ok": False, "reason": "Groups needs at most three label columns."}


def test_a_result_with_a_number_is_refused_as_a_chart_question(run):
    out = groups(run, "['name','n']", "[['a',1],['b',2],['c',3]]")
    assert out == {"ok": False, "reason": "Groups is for lists without a number. Use Bar, Pie or Line."}
    nulls = groups(run, "['name','n']", "[['a',1],['b',null],['c',3]]")
    assert nulls["reason"] == "Groups is for lists without a number. Use Bar, Pie or Line."


def test_a_year_column_is_not_a_number_for_the_groups_check(run):
    out = groups(run, "['name','hire_year']", "[['a',2012],['b',2013]]")
    assert out["ok"] is True


def test_a_year_plus_text_pair_uses_the_year_as_the_group(run):
    out = groups(run, "['year','name']", "[['2012','a'],['2013','b'],['2012','c']]")
    assert out["ok"] and out["groupName"] == "year" and out["itemNames"] == ["name"]
    assert out["groups"][0] == {"label": "2012", "items": ["a", "c"]}


def test_with_two_year_columns_the_last_is_the_group(run):
    rows = "[['a',2012,2020],['b',2012,2021],['c',2013,2021]]"
    out = groups(run, "['name','hire_year','end_year']", rows)
    assert out["ok"] and out["groupName"] == "end_year" and out["itemNames"] == ["name", "hire_year"]
    assert [g["label"] for g in out["groups"]] == ["2020", "2021"]
    assert out["groups"][1]["items"] == ["b 2012", "c 2013"]


def test_the_refusal_checks_run_in_the_order_rows_number_labels_groups(run):
    # one row AND a number AND one label: the row count is reported first
    assert groups(run, "['name','n']", "[['a',1]]")["reason"].startswith("Groups needs 2 to 60 rows")
    # a number AND one label column: the number reason comes before the label reason
    assert groups(run, "['n']", "[[1],[2]]")["reason"].startswith("Groups is for lists without a number")
    # one label column and one group value would fail on groups, but the label count is reported first
    assert groups(run, "['name']", "[['a'],['a']]")["reason"] == "Groups needs a group column and an item column."
    # four labels and one group: the label count comes before the group count
    assert groups(run, "['a','b','c','d']", "[['1','2','3','4'],['1','2','3','4']]")["reason"] == "Groups needs at most three label columns."


def test_availability_gains_the_groups_entry_and_the_others_are_unchanged(run):
    (av,) = run(f"availability(['fullname','hire_year'], {STAFF})")
    assert av["groups"] == "" and av["groupsInfo"]["rowCount"] == 9
    assert av["bar"] == av["pie"] == av["line"] == (
        "Charts need a number column to measure. hire_year is treated as a label. "
        "Ask for a count or total, for example: How many employees were hired each year?"
    )
    (num,) = run("availability(['name','n'], [['a',1],['b',2],['c',3]])")
    assert num["bar"] == "" and num["groups"] == "Groups is for lists without a number. Use Bar, Pie or Line."
    assert num["groupsInfo"] is None
    (one,) = run("availability(['a','n'], [['x',1]])")
    assert one["bar"] == "One row: the number is the answer, nothing to chart."
    assert one["groups"] == "Groups needs 2 to 60 rows; this has 1."


def test_availability_reason_for_many_rows_without_a_number(run):
    (av,) = run("availability(['customer','city'], Array.from({length:93}, (_, i) => ['c' + i, 'town' + (i % 5)]))")
    assert av["groups"] == "Groups needs 2 to 60 rows; this has 93."
    assert av["bar"] == "Charts need a number column to measure."


GROUPS_OK = {"bar": "r", "pie": "r", "line": "r", "groups": ""}
GROUPS_NO = {"bar": "", "pie": "", "line": "", "groups": "no groups"}


def test_pick_view_with_groups(run):
    out = run(
        pick("", GROUPS_OK),
        pick("", GROUPS_NO),
        pick("", {"bar": "", "pie": "", "line": "", "groups": ""}),
        pick("pie", GROUPS_OK),
        pick("bar", GROUPS_OK),
        pick("line", {"bar": "", "pie": "p", "line": "l", "groups": ""}),
        pick("pie", {"bar": "b", "pie": "p", "line": "l", "groups": "g"}),
        pick("bar", GROUPS_NO),
    )
    assert out[0] == {"view": "groups", "note": ""}
    assert out[1] == {"view": "table", "note": ""}
    assert out[2] == {"view": "groups", "note": ""}
    assert out[3] == {"view": "groups", "note": "r Showing groups instead."}
    assert out[4] == {"view": "groups", "note": "r Showing groups instead."}
    assert out[5] == {"view": "bar", "note": "l Showing a bar chart instead."}
    assert out[6] == {"view": "table", "note": "p Showing the table instead."}
    assert out[7] == {"view": "bar", "note": "Showing a bar chart because your question asked for one."}


def test_a_keyword_whose_view_is_available_still_wins(run):
    out = run(pick("line", {"bar": "", "pie": "", "line": "", "groups": ""}), pick("pie", {"bar": "", "pie": "", "line": "", "groups": ""}))
    assert out[0]["view"] == "line" and out[1]["view"] == "pie"


def test_an_availability_object_without_a_groups_entry_behaves_as_before(run):
    out = run(pick("", {"bar": "", "pie": "", "line": ""}), pick("pie", {"bar": "", "pie": "p", "line": ""}))
    assert out[0] == {"view": "table", "note": ""}
    assert out[1]["view"] == "bar"
