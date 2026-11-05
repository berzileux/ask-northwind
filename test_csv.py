import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed; the CSV logic tests run it")

SOURCE = (Path(__file__).parent / "index.html").read_text()
BEGIN = "/* CSV-LOGIC BEGIN */"
END = "/* CSV-LOGIC END */"

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
    folder = tmp_path_factory.mktemp("csv")
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


BOM = chr(0xFEFF)


def csv(run, columns, rows):
    (out,) = run(f"csvText({json.dumps(columns)}, {json.dumps(rows)})")
    return out


def test_markers_exist_once_in_order():
    assert SOURCE.count(BEGIN) == 1 and SOURCE.count(END) == 1
    assert SOURCE.index(BEGIN) < SOURCE.index(END)


def test_block_uses_no_document_window_navigator_storage_or_fetch():
    assert not re.search(r"\b(document|window|navigator|localStorage|fetch)\b", block())


def test_block_defines_its_functions():
    for name in ("csvField", "csvText", "csvFileName"):
        assert f"function {name}(" in block()


def test_a_plain_result_has_a_bom_a_header_crlf_and_a_final_crlf(run):
    text = csv(run, ["name", "n"], [["a", 1], ["b", 2]])
    assert text == BOM + "name,n\r\na,1\r\nb,2\r\n"


def test_the_bom_is_present_once_at_the_start(run):
    text = csv(run, ["a"], [["x"]])
    assert text.startswith(BOM) and text.count(BOM) == 1


def test_every_line_ends_with_crlf_and_there_is_no_bare_lf(run):
    text = csv(run, ["a", "b"], [["x", "y"], ["z", "w"]])
    assert text.endswith("\r\n")
    assert "\n" not in text.replace("\r\n", "")


def test_zero_rows_gives_the_header_line_only(run):
    assert csv(run, ["a", "b"], []) == BOM + "a,b\r\n"


@pytest.mark.parametrize(
    "value, field",
    [
        ("a,b", '"a,b"'),
        ('say "hi"', '"say ""hi"""'),
        ("line1\nline2", '"line1\nline2"'),
        ("line1\r\nline2", '"line1\r\nline2"'),
        ("a\rb", '"a\rb"'),
        ('mixed, "all"\nof it', '"mixed, ""all""\nof it"'),
        ('"', '""""'),
        ("plain", "plain"),
        ("with space", "with space"),
    ],
)
def test_quoting(run, value, field):
    assert csv(run, ["c"], [[value]]) == BOM + "c\r\n" + field + "\r\n"


def test_null_and_undefined_are_empty_fields(run):
    (out,) = run("csvText(['a','b','c'], [[null, undefined, 'x']])")
    assert out == BOM + "a,b,c\r\n,,x\r\n"


def test_numbers_are_written_as_returned(run):
    (out,) = run("csvText(['n'], [[5],[0],[1234567],[3.14159],[-5],[-0.5],[1e21],[0.1]])")
    assert out == BOM + "n\r\n5\r\n0\r\n1234567\r\n3.14159\r\n-5\r\n-0.5\r\n1e+21\r\n0.1\r\n"


def test_a_very_large_integer_sent_as_a_string_is_written_as_text(run):
    assert csv(run, ["n"], [["9007199254740993"]]) == BOM + "n\r\n9007199254740993\r\n"


def test_a_negative_number_is_never_prefixed_but_a_negative_looking_string_is(run):
    assert csv(run, ["n"], [[-5]]) == BOM + "n\r\n-5\r\n"
    assert csv(run, ["n"], [["-5"]]) == BOM + "n\r\n'-5\r\n"


def test_booleans(run):
    assert csv(run, ["b"], [[True], [False]]) == BOM + "b\r\ntrue\r\nfalse\r\n"


@pytest.mark.parametrize("first", ["=", "+", "-", "@"])
def test_a_string_starting_with_a_formula_character_is_prefixed(run, first):
    value = first + "1+1"
    assert csv(run, ["c"], [[value]]) == BOM + "c\r\n'" + value + "\r\n"


def test_a_string_starting_with_a_tab_is_prefixed(run):
    assert csv(run, ["c"], [["\tcmd"]]) == BOM + "c\r\n'\tcmd\r\n"


def test_a_string_starting_with_a_carriage_return_is_prefixed_inside_the_quotes(run):
    assert csv(run, ["c"], [["\rcmd"]]) == BOM + "c\r\n\"'\rcmd\"\r\n"


def test_the_prefix_goes_inside_the_quotes_when_the_field_is_quoted(run):
    assert csv(run, ["c"], [["=SUM(A1,A2)"]]) == BOM + "c\r\n\"'=SUM(A1,A2)\"\r\n"


def test_a_string_with_a_space_before_the_equals_sign_is_not_prefixed(run):
    assert csv(run, ["c"], [[" =1+1"]]) == BOM + "c\r\n =1+1\r\n"


def test_a_string_that_only_contains_a_formula_character_later_is_not_prefixed(run):
    assert csv(run, ["c"], [["a=b"], ["x-y"], ["me@home"]]) == BOM + "c\r\na=b\r\nx-y\r\nme@home\r\n"


def test_a_header_starting_with_equals_is_prefixed(run):
    assert csv(run, ["=bad", "ok"], [["x", "y"]]) == BOM + "'=bad,ok\r\nx,y\r\n"


def test_every_header_gets_the_same_protection_and_quoting(run):
    assert csv(run, ["+a", "-b", "@c", "d,e"], []) == BOM + "'+a,'-b,'@c,\"d,e\"\r\n"


def test_unicode_text_round_trips(run):
    cedilla, tokyo, umlaut = "C" + chr(0xE7) + "a va", chr(0x6771) + chr(0x4EAC), "Th" + chr(0xFC) + "ringer"
    text = csv(run, ["name"], [["Cote de Blaye"], [cedilla], [tokyo], [umlaut]])
    assert cedilla in text and tokyo in text and umlaut in text


def test_the_file_name_pattern(run):
    (name,) = run("csvFileName(new Date(2026, 10, 5, 7, 8, 9))")
    assert name == "ask-northwind-result-20261105-070809.csv"
    (name2,) = run("csvFileName(new Date(2026, 0, 31, 23, 59, 59))")
    assert name2 == "ask-northwind-result-20260131-235959.csv"
    assert re.fullmatch(r"ask-northwind-result-\d{8}-\d{6}\.csv", name)


def test_the_text_has_no_non_ascii_characters_in_the_source():
    SOURCE.encode("ascii")
