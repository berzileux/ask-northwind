import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed; the intent logic tests run it")

SOURCE = (Path(__file__).parent / "index.html").read_text()
BEGIN = "/* INTENT-LOGIC BEGIN */"
END = "/* INTENT-LOGIC END */"

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
    folder = tmp_path_factory.mktemp("intent")
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


def wants(run, *questions):
    return run(*[f"wantsDiagram({json.dumps(q)})" for q in questions])


def test_markers_exist_once_in_order():
    assert SOURCE.count(BEGIN) == 1 and SOURCE.count(END) == 1
    assert SOURCE.index(BEGIN) < SOURCE.index(END)


def test_block_uses_no_document_window_navigator_storage_or_fetch():
    assert not re.search(r"\b(document|window|navigator|localStorage|fetch)\b", block())


def test_the_two_word_lists_are_named_constants_with_a_reason_each():
    text = block()
    assert 'var DIAGRAM_WORDS = ["diagram", "diagrams", "erd", "er diagram", "entity relationship", "entity-relationship", "schematic", "schema diagram"];' in text
    assert 'var CHART_WORDS = ["pie", "bar", "line", "chart", "charts", "graph", "plot", "histogram"];' in text
    assert text.index("DIAGRAM_WORDS") < text.index("function wantsDiagram")
    before_chart = [line for line in text.split("var CHART_WORDS")[0].splitlines() if line.strip()]
    assert "chart word" in before_chart[-1]


@pytest.mark.parametrize(
    "question",
    [
        "can draw me a diagram from this",
        "schematic diagram on how these tables relate to each other",
        "ERD please",
        "Show the ER diagram",
        "entity relationship diagram",
        "entity-relationship diagram of northwind",
        "can you provide me a diagram in png of these tables",
        "DIAGRAM",
        "I have been wondering for a while, if you could kindly give me a diagram of the whole thing, thanks",
        "schematic",
        "entity relationship",
        "entity   relationship   please",
        "Diagrams of the tables",
        "schema diagram",
        "erd",
        "the erd, please",
        "diagram?",
        "(diagram)",
    ],
)
def test_diagram_requests_are_recognised(run, question):
    assert wants(run, question) == [True]


@pytest.mark.parametrize(
    "question",
    [
        "draw a pie chart of revenue by category",
        "show orders per year as a line chart",
        "bar chart of customers by country",
        "plot a diagram of sales",
        "graph the schema diagram",
        "how many orders are there",
        "list me its relationships with its tables",
        "airline customers",
        "baseline report",
        "diagramming tools",
        "nerd",
        "herd of customers",
        "",
        "   ",
        "\n\t ",
        "diagram of the line",
        "a histogram diagram",
        "charts and a diagram",
        "pie diagram",
        "ER",
        "entity",
        "relationship",
        "schematics",
        "diagrammatic",
    ],
)
def test_other_questions_are_not_diagram_requests(run, question):
    assert wants(run, question) == [False]


def test_chart_words_win_in_every_form(run):
    for word in ("pie", "bar", "line", "chart", "charts", "graph", "plot", "histogram", "PIE", "Line"):
        assert wants(run, f"diagram {word}") == [False], word
        assert wants(run, f"{word}, diagram") == [False], word


def test_whole_word_matching_for_the_chart_words(run):
    # "line" inside longer words does not count, so these are still diagram requests
    assert wants(run, "baseline diagram") == [True]
    assert wants(run, "airline diagram") == [True]
    assert wants(run, "barcode diagram") == [True]
    assert wants(run, "plotted diagram") == [True]
    assert wants(run, "a line diagram") == [False]


def test_empty_null_and_undefined_inputs_are_false(run):
    assert run("wantsDiagram(null)", "wantsDiagram(undefined)", "wantsDiagram('')", "wantsDiagram(5)") == [False] * 4


def test_only_the_visitors_text_is_looked_at(run):
    # a bare request works because its own words say it; nothing else is consulted
    assert wants(run, "can draw me a diagram from this") == [True]
    assert "history" not in block() and "turns" not in block() and "exchanges" not in block()
