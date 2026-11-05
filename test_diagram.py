import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed; the diagram logic tests run it")

SOURCE = (Path(__file__).parent / "index.html").read_text()
BEGIN = "/* DIAGRAM-LOGIC BEGIN */"
END = "/* DIAGRAM-LOGIC END */"

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
    folder = tmp_path_factory.mktemp("diagram")
    (folder / "logic.js").write_text(block())
    (folder / "runner.js").write_text(RUNNER)

    def evaluate(*expressions):
        (folder / "exprs.json").write_text(json.dumps(list(expressions)))
        done = subprocess.run(
            [NODE, str(folder / "runner.js"), str(folder / "logic.js"), str(folder / "exprs.json")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)

    return evaluate


@pytest.fixture(scope="module")
def real_schema(real_db_path):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(app, "DB_PATH", real_db_path)
        with TestClient(app.app) as client:
            return client.get("/api/schema").json()


def table(name, keys=(), columns=("id",)):
    return {
        "name": name,
        "columns": [{"name": c, "type": "TEXT"} for c in columns],
        "foreign_keys": [{"column": c, "ref_table": t, "ref_column": r} for c, t, r in keys],
    }


def layout(run, schema, options=None):
    opts = json.dumps(options) if options is not None else "undefined"
    (out,) = run(f"diagramLayout({json.dumps(schema)}, {opts})")
    return out


def boxes(out):
    return {b["name"]: b for b in out["boxes"]}


def test_markers_exist_once_in_order():
    assert SOURCE.count(BEGIN) == 1 and SOURCE.count(END) == 1
    assert SOURCE.index(BEGIN) < SOURCE.index(END)


def test_block_uses_no_document_window_navigator_storage_or_fetch():
    assert not re.search(r"\b(document|window|navigator|localStorage|fetch)\b", block())


def test_block_defines_its_functions():
    for name in ("diagramLayout", "countCrossings"):
        assert f"function {name}(" in block()


def test_the_real_schema_has_the_pinned_layers(run, real_schema):
    out = layout(run, real_schema)
    layers = [sorted(col) for col in out["layers"]]
    assert layers == [
        sorted(["Categories", "Suppliers", "Shippers", "Customers", "CustomerDemographics", "Employees", "Regions"]),
        sorted(["Products", "Orders", "CustomerCustomerDemo", "Territories"]),
        sorted(["Order Details", "EmployeeTerritories"]),
    ]
    assert len(out["boxes"]) == 13 and len(out["links"]) == 13
    assert [l["self"] for l in out["links"]].count(True) == 1
    selfs = [l for l in out["links"] if l["self"]]
    assert selfs[0]["from"] == selfs[0]["to"] == "Employees" and selfs[0]["column"] == "ReportsTo"


def test_the_real_schema_boxes_are_inside_the_bounds_and_do_not_overlap(run, real_schema):
    out = layout(run, real_schema)
    for b in out["boxes"]:
        assert b["x"] >= 24 and b["y"] >= 24
        assert b["x"] + b["w"] <= out["width"] - 24 and b["y"] + b["h"] <= out["height"] - 24
        assert b["w"] == 200 and b["h"] == 46
    for i, a in enumerate(out["boxes"]):
        for b in out["boxes"][i + 1:]:
            apart = a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"] or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"]
            assert apart, (a["name"], b["name"])


def test_the_geometry_constants(run, real_schema):
    out = layout(run, real_schema)
    xs = sorted({b["x"] for b in out["boxes"]})
    assert xs == [24, 304, 584]
    assert out["width"] == 24 * 2 + 3 * 200 + 2 * 80 == 808
    ys = sorted({b["y"] for b in out["boxes"] if b["layer"] == 0})
    assert all(b - a == 62 for a, b in zip(ys, ys[1:]))
    assert out["height"] == 24 * 2 + 6 * 62 + 46


def test_the_page_layout_has_no_more_crossings_than_alphabetical_order(run, real_schema):
    page, alpha = run(
        f"countCrossings(diagramLayout({json.dumps(real_schema)}))",
        f"countCrossings(diagramLayout({json.dumps(real_schema)}, {{sweeps: 0}}))",
    )
    print("crossings: page layout", page, "alphabetical", alpha)
    assert page <= alpha


def test_layout_is_identical_on_a_second_call(run, real_schema):
    a, b = run(f"JSON.stringify(diagramLayout({json.dumps(real_schema)}))", f"JSON.stringify(diagramLayout({json.dumps(real_schema)}))")
    assert a == b


def test_links_run_from_the_left_edge_of_the_holder_to_the_right_edge_of_the_target(run, real_schema):
    out = layout(run, real_schema)
    at = boxes(out)
    for link in out["links"]:
        if link["self"]:
            continue
        a, b, p = at[link["from"]], at[link["to"]], link["path"]
        assert (p["x1"], p["y1"]) == (a["x"], a["y"] + 23)
        assert (p["x2"], p["y2"]) == (b["x"] + 200, b["y"] + 23)
        assert link["dot"] == {"x": p["x1"], "y": p["y1"]}
        assert p["c1y"] == p["y1"] and p["c2y"] == p["y2"]
        assert p["c1x"] < p["x1"] and p["c2x"] > p["x2"]
        assert b["layer"] < a["layer"]


def test_the_self_link_is_a_loop_on_the_right_and_inside_the_bounds(run, real_schema):
    out = layout(run, real_schema)
    loop = next(l for l in out["links"] if l["self"])["path"]
    box = boxes(out)["Employees"]
    assert loop["x1"] == loop["x2"] == box["x"] + 200
    assert loop["c1x"] == loop["c2x"] == box["x"] + 230
    assert loop["y1"] != loop["y2"]
    assert max(loop["c1x"], loop["c2x"]) <= out["width"]


def test_each_link_carries_its_columns(run, real_schema):
    out = layout(run, real_schema)
    triples = sorted((l["from"], l["column"], l["to"], l["refColumn"]) for l in out["links"])
    expected = sorted(
        (t["name"], k["column"], k["ref_table"], k["ref_column"]) for t in real_schema["tables"] for k in t["foreign_keys"]
    )
    assert triples == expected


def test_the_subtitles_for_the_real_schema(run, real_schema):
    at = boxes(layout(run, real_schema))
    assert at["Products"]["subtitle"] == "FK: CategoryID, SupplierID"
    assert at["Employees"]["subtitle"] == "FK: ReportsTo"
    assert at["Categories"]["subtitle"] == "referenced by 1"
    assert at["Customers"]["subtitle"] == "referenced by 2"
    assert at["Orders"]["fullSubtitle"] == "FK: CustomerID, EmployeeID, ShipVia"
    assert len("FK: CustomerID, EmployeeID, ShipVia") > 28
    assert at["Orders"]["subtitle"] == "FK: CustomerID, EmployeeI..."
    assert at["Orders"]["subtitle"].endswith("...") and len(at["Orders"]["subtitle"]) == 28
    for b in at.values():
        assert len(b["subtitle"]) <= 28 and b["title"] == b["name"]
        b["subtitle"].encode("ascii")


def test_no_foreign_keys_at_all(run):
    out = layout(run, {"tables": [table("B"), table("A"), table("C")]})
    assert out["links"] == [] and len(out["boxes"]) == 3
    assert [col for col in out["layers"]] == [["A", "B", "C"]]
    assert all(b["subtitle"] == "referenced by 0" for b in out["boxes"])
    assert out["width"] == 24 * 2 + 200 and out["height"] == 24 * 2 + 2 * 62 + 46


def test_a_single_table(run):
    out = layout(run, {"tables": [table("Only")]})
    assert len(out["boxes"]) == 1 and out["links"] == []
    assert out["boxes"][0]["x"] == 24 and out["boxes"][0]["y"] == 24
    assert out["width"] == 248 and out["height"] == 94


def test_an_empty_schema_has_only_the_padding(run):
    out = layout(run, {"tables": []})
    assert out["boxes"] == [] and out["links"] == [] and out["width"] == 48 and out["height"] == 48
    assert layout(run, {})["boxes"] == []


def test_two_tables_referencing_each_other_terminate_and_both_links_exist(run):
    schema = {"tables": [table("A", [("b", "B", "id")]), table("B", [("a", "A", "id")])]}
    out = layout(run, schema)
    assert len(out["boxes"]) == 2 and len(out["links"]) == 2
    for b in out["boxes"]:
        assert 0 <= b["layer"] <= 1
    assert out == layout(run, schema)


def test_a_longer_cycle_terminates(run):
    schema = {"tables": [table("A", [("c", "C", "id")]), table("B", [("a", "A", "id")]), table("C", [("b", "B", "id")])]}
    out = layout(run, schema)
    assert len(out["boxes"]) == 3 and len(out["links"]) == 3
    assert all(0 <= b["layer"] <= 2 for b in out["boxes"])


def test_two_foreign_keys_between_the_same_pair_make_two_links(run):
    schema = {"tables": [table("Person"), table("Flight", [("pilot", "Person", "id"), ("copilot", "Person", "id")])]}
    out = layout(run, schema)
    assert len(out["links"]) == 2
    assert sorted(l["column"] for l in out["links"]) == ["copilot", "pilot"]
    assert boxes(out)["Person"]["subtitle"] == "referenced by 2"
    assert boxes(out)["Flight"]["subtitle"] == "FK: pilot, copilot"
    assert boxes(out)["Flight"]["layer"] == 1 and boxes(out)["Person"]["layer"] == 0


def test_a_reference_to_a_missing_table_is_skipped(run):
    schema = {"tables": [table("A", [("x", "Ghost", "id"), ("y", "B", "id")]), table("B")]}
    out = layout(run, schema)
    assert [(l["from"], l["to"]) for l in out["links"]] == [("A", "B")]
    assert boxes(out)["A"]["subtitle"] == "FK: y"


def test_a_table_name_with_a_space(run):
    schema = {"tables": [table("Order Details", [("OrderID", "Big Parent", "id")]), table("Big Parent")]}
    out = layout(run, schema)
    assert boxes(out)["Order Details"]["title"] == "Order Details"
    assert out["links"][0]["from"] == "Order Details" and out["links"][0]["to"] == "Big Parent"


def test_a_very_long_column_list_is_cut_and_the_full_text_is_kept(run):
    keys = [(f"column_number_{i}", "P", "id") for i in range(12)]
    out = layout(run, {"tables": [table("P"), table("Wide", keys)]})
    wide = boxes(out)["Wide"]
    assert len(wide["subtitle"]) == 28 and wide["subtitle"].endswith("...")
    assert wide["fullSubtitle"].startswith("FK: column_number_0, column_number_1")
    assert wide["fullSubtitle"].count("column_number_") == 12


def test_a_self_reference_does_not_change_the_layer(run):
    out = layout(run, {"tables": [table("Tree", [("parent", "Tree", "id")])]})
    assert out["boxes"][0]["layer"] == 0 and out["links"][0]["self"] is True


def test_ties_are_broken_by_name_so_input_order_does_not_matter(run):
    a = layout(run, {"tables": [table("B"), table("A"), table("C", [("a", "A", "id")])]})
    b = layout(run, {"tables": [table("C", [("a", "A", "id")]), table("A"), table("B")]})
    assert a == b


def test_crossings_helper_counts_an_x_and_ignores_shared_ends(run):
    crossed = {"links": [
        {"self": False, "path": {"x1": 0, "y1": 0, "x2": 10, "y2": 10}},
        {"self": False, "path": {"x1": 0, "y1": 10, "x2": 10, "y2": 0}},
    ]}
    shared = {"links": [
        {"self": False, "path": {"x1": 0, "y1": 0, "x2": 10, "y2": 10}},
        {"self": False, "path": {"x1": 0, "y1": 0, "x2": 10, "y2": 0}},
    ]}
    (a, b) = run(f"countCrossings({json.dumps(crossed)})", f"countCrossings({json.dumps(shared)})")
    assert a == 1 and b == 0
