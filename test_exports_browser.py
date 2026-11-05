import json
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app

# Like the chart smoke test, the page is loaded in a real headless browser with every
# request answered by a stub, so no server and no model are involved. The browser tool
# lives outside the project environment, so a separate interpreter runs the script.
SCRIPT = r"""
import base64, json, sys
from playwright.sync_api import sync_playwright

payload = json.load(open(sys.argv[1]))
folder = sys.argv[2]

def answer(q, cols, rows, truncated=False):
    return {"question": q, "sql": "SELECT 1", "columns": cols, "rows": rows, "truncated": truncated, "elapsed_s": 0.1, "query_s": 0.02,
            "error": None, "error_kind": None, "source": "model", "limits": {"timeout_s": 5.0, "max_rows": 500}}

NINE = [["Emp%d" % i, "2012", i * 1.5] for i in range(9)]
LONG = "About this database. " + "This sentence makes the text longer than one hundred and twenty characters. " * 3
TABLES = [
    {"name": "Customers", "columns": [{"name": "CustomerID", "type": "TEXT"}, {"name": "CompanyName", "type": "TEXT"}], "foreign_keys": []},
    {"name": "Orders", "columns": [{"name": "OrderID", "type": "INTEGER"}, {"name": "CustomerID", "type": "TEXT"}], "foreign_keys": [{"column": "CustomerID", "ref_table": "Customers", "ref_column": "CustomerID"}]},
    {"name": "Order Details", "columns": [{"name": "OrderID", "type": "INTEGER"}, {"name": "ProductID", "type": "INTEGER"}], "foreign_keys": [{"column": "OrderID", "ref_table": "Orders", "ref_column": "OrderID"}]},
]

def stub(q):
    if "csv nine" in q: return answer(q, ["name", "year", "score"], NINE)
    if "csv formula" in q: return answer(q, ["name", "note"], [["=1+1", "plain"], ["-x", "a,b"], ["ok", "say \"hi\""]])
    if "csv cut" in q: return answer(q, ["name", "n"], [["row%d" % i, i] for i in range(500)], True)
    if "csv refusal" in q: return answer(q, ["message"], [["I cannot answer that from this database"]])
    if "csv about" in q: return answer(q, ["about"], [[LONG]])
    return answer(q, ["name", "n"], [["a", 1], ["b", 2]])

errors = []
out = {}
with sync_playwright() as p:
    try:
        browser = p.chromium.launch()
    except Exception as exc:
        print("BROWSER_UNAVAILABLE " + str(exc).splitlines()[0])
        sys.exit(3)
    for scheme, width in (("light", 1280), ("dark", 1280), ("light", 420)):
        ctx = browser.new_context(viewport={"width": width, "height": 900}, accept_downloads=True)
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
        def handler(route):
            req = route.request
            path = req.url.split("app.test", 1)[1]
            if path == "/":
                route.fulfill(status=200, content_type="text/html", body=payload["html"], headers={"Content-Security-Policy": payload["csp"]})
            elif path == "/api/limits":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"timeout_s": 5.0, "max_rows": 500, "slots": 1, "memory_turns": 4, "rate_limit_per_minute": 10}))
            elif path == "/api/sandbox-demos":
                route.fulfill(status=200, content_type="application/json", body="[]")
            elif path == "/api/schema":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"tables": TABLES}))
            elif path == "/api/about":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"text": LONG}))
            elif path == "/api/ask":
                q = json.loads(req.post_data)["question"]
                route.fulfill(status=200, content_type="application/json", body=json.dumps(stub(q)))
            else:
                route.fulfill(status=404, body="")
        page.route("http://app.test/**", handler)
        page.goto("http://app.test/")
        page.click('.seg button[data-t="%s"]' % scheme)
        key = scheme + str(width)
        res = {}
        def ask(q):
            page.fill("#q", q)
            page.press("#q", "Enter")
            page.wait_for_selector(".reply:last-of-type .pill")
        def last():
            return page.locator(".reply").last
        def download(button):
            with page.expect_download() as info:
                button.click()
            dl = info.value
            target = folder + "/" + key + "_" + dl.suggested_filename
            dl.save_as(target)
            return dl.suggested_filename, target

        ask("csv nine rows")
        res["csv_buttons"] = last().locator("button", has_text="CSV").count()
        name, target = download(last().locator("button", has_text="CSV"))
        res["csv_name"] = name
        res["csv_bytes_hex"] = open(target, "rb").read().hex()
        res["csv_header_row_has_button"] = last().locator(".reply-top button", has_text="CSV").count()

        ask("csv formula test")
        name, target = download(last().locator("button", has_text="CSV"))
        res["formula_bytes_hex"] = open(target, "rb").read().hex()

        ask("csv cut rows")
        res["cut_note"] = last().locator(".reply-top .meta").all_inner_texts()
        name, target = download(last().locator("button", has_text="CSV"))
        res["cut_lines"] = open(target, "rb").read().decode("utf-8").count("\r\n")

        ask("csv refusal please")
        res["refusal_hint"] = [e.inner_text() for e in last().locator(".note").all()]
        res["refusal_csv"] = last().locator("button", has_text="CSV").count()
        ask("csv normal answer")
        res["normal_hints"] = [e.inner_text() for e in last().locator(".note").all() if "Database map shows" in e.inner_text()]
        res["normal_csv"] = last().locator("button", has_text="CSV").count()
        ask("csv about paragraph")
        res["paragraph_csv"] = last().locator("button", has_text="CSV").count()
        res["paragraph_class"] = last().locator("p.paragraph").count()

        # the built-in entries
        page.click("#examples summary")
        page.locator("#groups .chip", has_text="About this database").click()
        page.wait_for_selector(".reply:last-of-type p.paragraph")
        res["about_csv"] = last().locator("button", has_text="CSV").count()
        res["about_image_button"] = last().locator("button", has_text="Save as image").count()
        page.locator("#groups .chip", has_text="Database map").click()
        page.wait_for_selector(".reply:last-of-type .dg-svg")
        card = last()
        res["map_csv"] = card.locator("button", has_text="CSV").count()
        res["map_image_buttons"] = card.locator("button", has_text="Save as image").count()
        res["map_width"] = card.locator("svg.dg-svg").bounding_box()["width"]
        name, target = download(card.locator("button", has_text="Save as image"))
        data = open(target, "rb").read()
        res["png_name"] = name
        res["png_head_hex"] = data[:8].hex()
        res["png_size"] = len(data)
        res["png_width"] = int.from_bytes(data[16:20], "big")
        res["png_height"] = int.from_bytes(data[20:24], "big")
        # sample pixels in a page with no policy
        sampler = ctx.new_page()
        sampler.set_content("<canvas id=c></canvas>")
        pixel_js = (
            "async (b64) => {"
            " const img = new Image();"
            " img.src = 'data:image/png;base64,' + b64;"
            " await img.decode();"
            " const c = document.getElementById('c');"
            " c.width = img.width; c.height = img.height;"
            " const x = c.getContext('2d'); x.drawImage(img, 0, 0);"
            " const px = (a, b) => Array.from(x.getImageData(a, b, 1, 1).data);"
            " const all = x.getImageData(0, 0, img.width, img.height).data;"
            " const bg = px(2, 2);"
            " let other = 0;"
            " for (let i = 0; i < all.length; i += 4) { if (Math.abs(all[i] - bg[0]) > 40) other++; }"
            " return { corner: bg, corner2: px(img.width - 3, img.height - 3), other: other };"
            " }"
        )
        res["png_pixels"] = sampler.evaluate(pixel_js, base64.b64encode(data).decode())
        sampler.close()
        res["map_note_hidden"] = card.locator(".note").first.is_hidden()

        # forced export failures
        page.evaluate("(() => { window.__realToBlob = HTMLCanvasElement.prototype.toBlob; HTMLCanvasElement.prototype.toBlob = function (cb) { cb(null); }; })()")
        card.locator("button", has_text="Save as image").click()
        res["image_failure_note"] = card.locator(".note").first.inner_text()
        page.evaluate("(() => { HTMLCanvasElement.prototype.toBlob = window.__realToBlob; })()")
        ask("csv nine rows again")
        page.evaluate("(() => { window.__realCreate = URL.createObjectURL; URL.createObjectURL = function () { throw new Error('blocked'); }; })()")
        last().locator("button", has_text="CSV").click()
        res["csv_failure_note"] = last().locator(".note").last.inner_text()
        page.evaluate("(() => { URL.createObjectURL = window.__realCreate; })()")
        res["overflow"] = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        out[key] = res
        ctx.close()
    browser.close()
out["errors"] = errors
print(json.dumps(out))
"""


def find_interpreter():
    for exe in (shutil.which("python3"), shutil.which("python"), "/usr/bin/python3"):
        if not exe:
            continue
        probe = subprocess.run([exe, "-c", "import playwright.sync_api"], capture_output=True)
        if probe.returncode == 0:
            return exe
    return None


INTERPRETER = find_interpreter()
pytestmark = pytest.mark.skipif(
    INTERPRETER is None, reason="no interpreter with playwright importable; the export browser test is skipped"
)

SCENES = ["light1280", "dark1280", "light420"]
BOM = chr(0xFEFF)


@pytest.fixture(scope="module")
def results(tmp_path_factory, db_path):
    folder = tmp_path_factory.mktemp("exports")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(app, "DB_PATH", db_path)
        with TestClient(app.app) as client:
            response = client.get("/")
    (folder / "payload.json").write_text(
        json.dumps({"html": response.text, "csp": response.headers["Content-Security-Policy"]})
    )
    (folder / "smoke.py").write_text(SCRIPT)
    done = subprocess.run(
        [INTERPRETER, str(folder / "smoke.py"), str(folder / "payload.json"), str(folder)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if done.returncode == 3 and "BROWSER_UNAVAILABLE" in done.stdout:
        pytest.skip("headless browser is not installed: " + done.stdout.strip())
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


def decode(hex_text):
    return bytes.fromhex(hex_text).decode("utf-8")


def test_no_console_or_page_errors_and_the_csp_is_respected(results):
    assert results["errors"] == []


@pytest.mark.parametrize("scene", SCENES)
def test_the_csv_button_downloads_exactly_csv_text_for_the_rows(results, scene):
    r = results[scene]
    assert r["csv_buttons"] == 1 and r["csv_header_row_has_button"] == 1
    assert re.fullmatch(r"ask-northwind-result-\d{8}-\d{6}\.csv", r["csv_name"])
    # The page writes numbers the way JavaScript prints them, so 3.0 is written as 3.
    expected = BOM + "name,year,score\r\n" + "".join(f"Emp{i},2012,{i * 1.5:g}\r\n" for i in range(9))
    assert decode(r["csv_bytes_hex"]) == expected
    assert bytes.fromhex(r["csv_bytes_hex"])[:3] == b"\xef\xbb\xbf"


@pytest.mark.parametrize("scene", SCENES)
def test_a_cell_starting_with_equals_is_exported_with_the_apostrophe(results, scene):
    text = decode(results[scene]["formula_bytes_hex"])
    assert text == BOM + "name,note\r\n'=1+1,plain\r\n'-x,\"a,b\"\r\nok,\"say \"\"hi\"\"\"\r\n"


@pytest.mark.parametrize("scene", SCENES)
def test_a_cut_result_says_the_csv_holds_the_rows_shown_and_has_exactly_those_rows(results, scene):
    r = results[scene]
    assert "CSV holds the 500 rows shown." in r["cut_note"]
    assert r["cut_lines"] == 501


@pytest.mark.parametrize("scene", SCENES)
def test_the_refusal_row_shows_the_hint_and_no_csv_button(results, scene):
    r = results[scene]
    hint = "I can only answer from this database's tables. Database map shows how they connect; About this database says what is in it."
    assert hint in r["refusal_hint"]
    assert r["refusal_csv"] == 0


@pytest.mark.parametrize("scene", SCENES)
def test_a_normal_answer_shows_no_hint_and_has_the_csv_button(results, scene):
    r = results[scene]
    assert r["normal_hints"] == [] and r["normal_csv"] == 1


@pytest.mark.parametrize("scene", SCENES)
def test_the_about_answer_paragraph_and_the_about_entry_have_no_csv_button(results, scene):
    r = results[scene]
    assert r["paragraph_class"] == 1 and r["paragraph_csv"] == 0
    assert r["about_csv"] == 0 and r["about_image_button"] == 0


@pytest.mark.parametrize("scene", SCENES)
def test_the_database_map_has_save_as_image_and_no_csv_button(results, scene):
    r = results[scene]
    assert r["map_image_buttons"] == 1 and r["map_csv"] == 0


@pytest.mark.parametrize("scene", SCENES)
def test_the_map_downloads_a_real_png_at_least_as_wide_as_the_map(results, scene):
    r = results[scene]
    assert r["png_name"] == "northwind-database-map.png"
    assert r["png_head_hex"] == "89504e470d0a1a0a"
    assert r["png_size"] > 2000
    assert r["png_width"] >= r["map_width"]
    assert r["png_height"] > 100
    assert r["map_note_hidden"] is True


@pytest.mark.parametrize("scene", ["light1280", "light420"])
def test_the_light_png_has_a_light_background_and_drawn_text(results, scene):
    pixels = results[scene]["png_pixels"]
    assert min(pixels["corner"][:3]) > 200 and min(pixels["corner2"][:3]) > 200
    assert pixels["other"] > 500


def test_the_dark_png_is_not_white_on_white(results):
    pixels = results["dark1280"]["png_pixels"]
    assert max(pixels["corner"][:3]) < 80 and max(pixels["corner2"][:3]) < 80
    assert pixels["other"] > 500


@pytest.mark.parametrize("scene", SCENES)
def test_a_failed_export_shows_a_plain_note(results, scene):
    r = results[scene]
    assert r["image_failure_note"] == "Could not save the image in this browser."
    assert r["csv_failure_note"] == "Could not save the file in this browser."


@pytest.mark.parametrize("scene", SCENES)
def test_nothing_overflows_sideways(results, scene):
    assert results[scene]["overflow"] <= 0
