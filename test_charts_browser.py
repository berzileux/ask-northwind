import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app

# The page is loaded in a real headless browser with every request answered by a
# stub, so no server and no model are involved. The browser tool lives outside the
# project environment, so it is run in a separate interpreter and the whole file is
# skipped when none is found.
SCRIPT = r"""
import json, sys
from playwright.sync_api import sync_playwright

payload = json.load(open(sys.argv[1]))
shots = None

def answer(q, cols, rows, truncated=False):
    return {"question": q, "sql": "SELECT 1", "columns": cols, "rows": rows, "truncated": truncated, "elapsed_s": 0.1, "query_s": 0.02,
            "error": None, "error_kind": None, "source": "model", "limits": {"timeout_s": 5.0, "max_rows": 500}}

YEARS = [[str(2012 + i), v] for i, v in enumerate([18823201.72, 38633120.01, 38870148.13, 48921330.4, 40568672.36, 52114873.6, 61450218.9, 58230944.15, 66781203.7, 71290455.2, 69884120.05, 77310562.8])]
CATS = [[n, v] for n, v in [("Beverages", 92163184.18), ("Confections", 66337803.06), ("Meat/Poultry", 64881147.97), ("Dairy Products", 60000000.0), ("Seafood", 50000000.0), ("Condiments", 40000000.0), ("Produce", 30000000.0), ("Grains/Cereals", 20000000.0)]]

LONG = "About this database. " + "This sentence makes the text longer than one hundred and twenty characters. " * 3

def failed(q, kind, text, **extra):
    d = answer(q, [], [])
    d.update({"sql": "", "error": text, "error_kind": kind})
    d.update(extra)
    return d

STAFF = [["Andrew Fuller", "2012"], ["Janet Leverling", "2012"], ["Nancy Davolio", "2012"], ["Margaret Peacock", "2013"], ["Michael Suyama", "2013"], ["Steven Buchanan", "2013"], ["Anne Dodsworth", "2014"], ["Laura Callahan", "2014"], ["Robert King", "2014"]]

def stub(q):
    if "groups fullname" in q: return answer(q, ["fullname", "hire_year"], STAFF)
    if "groups split" in q: return answer(q, ["FirstName", "LastName", "HireYear"], [[n.split(" ")[0], n.split(" ")[1], y] for n, y in STAFF])
    if "groups count" in q: return answer(q, ["category", "n"], [["Beverages", 3], ["Condiments", 5], ["Seafood", 2]])
    if "groups many" in q: return answer(q, ["customer", "city"], [["customer%d" % i, "town%d" % (i % 5)] for i in range(93)])
    if "quota test" in q: return failed(q, "quota", "The free model quota is used up for now. Please try again in a minute.")
    if "unavailable test" in q: return failed(q, "unavailable", "The model service is busy or slow right now. Please try again.")
    if "rate limit test" in q: return failed(q, "rate_limited", "You are asking too fast. Please wait 7 seconds.", retry_after_s=7, __status__=429)
    if "two part pie" in q: return answer(q, ["name", "n"], [["a", 3], ["b", 5]])
    if "split names" in q: return answer(q, ["FirstName", "LastName", "OrderCount"], [["Margaret", "Peacock", 1908], ["Nancy", "Davolio", 1000], ["Janet", "Leverling", 900]])
    if "hire year" in q: return answer(q, ["FirstName", "LastName", "HireYear"], [["Nancy", "Davolio", "2012"], ["Janet", "Leverling", "2013"], ["Margaret", "Peacock", "2014"]])
    if "database about" in q: return answer(q, ["about"], [[LONG]])
    if "pie chart of orders" in q: return answer(q, ["shipper", "orders"], [["United Package", 5486], ["Federal Shipping", 5461], ["Speedy Express", 5335]])
    if "line chart" in q: return answer(q, ["year", "revenue"], YEARS)
    if "by category" in q: return answer(q, ["category", "revenue"], CATS)
    if "discontinued" in q: return answer(q, ["discontinued"], [[8]])
    if "sold the most" in q: return answer(q, ["label", "employee", "revenue"], [["most", "A", 5.0], ["least", "B", 2.0]])
    if "cut" in q: return answer(q, ["name", "n"], [["row%d" % i, i + 1] for i in range(500)], True)
    return answer(q, ["customer", "revenue"], [["B's Beverages", 6154115.34], ["Hungry Coyote Import Store", 5698023.67], ["Rancho grande", 5559110.08], ["Gourmet Lanchonetes", 5552309.8], ["Ana Trujillo Emparedados y helados", 5534356.65]])

errors = []
out = {}
with sync_playwright() as p:
    try:
        browser = p.chromium.launch()
    except Exception as exc:
        print("BROWSER_UNAVAILABLE " + str(exc).splitlines()[0])
        sys.exit(3)
    for scheme, width in (("light", 1280), ("dark", 1280), ("light", 420)):
        ctx = browser.new_context(viewport={"width": width, "height": 900})
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
            elif path == "/api/about":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"text": LONG}))
            elif path == "/api/sandbox-demos":
                route.fulfill(status=200, content_type="application/json", body="[]")
            elif path == "/api/ask":
                q = json.loads(req.post_data)["question"]
                data = stub(q)
                status = data.pop("__status__", 200)
                route.fulfill(status=status, content_type="application/json", body=json.dumps(data), headers={"Retry-After": "7"} if status == 429 else {})
            else:
                route.fulfill(status=404, body="")
        page.route("http://app.test/**", handler)
        page.goto("http://app.test/")
        page.click(".seg button[data-t=\"%s\"]" % scheme)
        key = scheme + str(width)
        res = {}
        def ask(q):
            page.fill("#q", q)
            page.press("#q", "Enter")
            page.wait_for_selector(".reply:last-of-type .seg.viewseg")
        # chips
        res["theme_attr"] = page.evaluate("document.documentElement.getAttribute(\"data-theme\")")
        res["chips"] = [b.inner_text() for b in page.query_selector_all("#groups .group")[1].query_selector_all("button")] if len(page.query_selector_all("#groups .group")) > 1 else []
        res["group_names"] = [g.inner_text() for g in page.query_selector_all("#groups .group-name")]
        ask("Draw a pie chart of orders by shipper")
        reply = page.locator(".reply").last
        res["pie_pressed"] = reply.locator('.viewseg button[aria-pressed="true"]').inner_text()
        res["pie_slices"] = reply.locator(".slice").count()
        res["pie_legend"] = reply.locator(".legend li").count()
        if shots: page.screenshot(path=f"{shots}/pie_{key}.png")
        reply.locator('.viewseg button', has_text="Bar").click()
        res["bar_marks"] = reply.locator(".mark").count()
        if shots: page.screenshot(path=f"{shots}/bar_{key}.png")
        reply.locator('.viewseg button', has_text="Table").click()
        res["table_rows"] = reply.locator("tbody tr").count()
        res["line_disabled_on_3"] = reply.locator('.viewseg button', has_text="Line").is_disabled()
        ask("Show total revenue by year as a line chart")
        reply = page.locator(".reply").last
        res["line_pressed"] = reply.locator('.viewseg button[aria-pressed="true"]').inner_text()
        res["line_polyline"] = reply.locator("polyline.ln").count()
        overlay = reply.locator(".overlay")
        overlay.focus()
        res["tip_after_focus"] = reply.locator(".tip").first.is_visible()
        before = reply.locator(".tip strong").inner_text()
        page.keyboard.press("ArrowLeft")
        after = reply.locator(".tip strong").inner_text()
        res["arrow_moves"] = before != after
        res["tip_text"] = reply.locator(".tip").inner_text()
        if shots: page.screenshot(path=f"{shots}/line_{key}.png")
        ask("Draw a pie chart of revenue by category")
        reply = page.locator(".reply").last
        res["cat_pressed"] = reply.locator('.viewseg button[aria-pressed="true"]').inner_text()
        res["cat_note"] = reply.locator(".note").first.inner_text()
        res["cat_marks"] = reply.locator(".mark").count()
        res["cat_pie_disabled"] = reply.locator('.viewseg button', has_text="Pie").is_disabled()
        if shots: page.screenshot(path=f"{shots}/cat_{key}.png")
        ask("How many products are discontinued?")
        reply = page.locator(".reply").last
        res["one_row_disabled"] = [reply.locator('.viewseg button', has_text=t).is_disabled() for t in ("Bar", "Pie", "Line")]
        res["one_row_title"] = reply.locator('.viewseg button', has_text="Bar").get_attribute("title")
        res["one_row_note"] = reply.locator(".note").first.inner_text()
        ask("Which employee sold the most, and which sold the least?")
        reply = page.locator(".reply").last
        res["three_col_disabled"] = [reply.locator('.viewseg button', has_text=t).is_disabled() for t in ("Bar", "Pie", "Line")]
        res["three_col_pressed"] = reply.locator('.viewseg button[aria-pressed="true"]').inner_text()
        res["three_col_table_enabled"] = not reply.locator('.viewseg button', has_text="Table").is_disabled()
        reply.click()
        res["report_has_chart_views"] = page.locator("#report").inner_text().lower().count("chart views")
        ask("Top 5 customers by revenue as a bar chart")
        reply = page.locator(".reply").last
        res["bar_pressed"] = reply.locator('.viewseg button[aria-pressed="true"]').inner_text()
        res["bar_mark_tabindex"] = reply.locator(".mark").first.get_attribute("tabindex")
        reply.locator(".mark").first.focus()
        res["bar_tip_on_focus"] = reply.locator(".tip").first.is_visible()
        res["bar_aria"] = reply.locator(".mark").first.get_attribute("aria-label")
        ask("show cut rows graph")
        reply = page.locator(".reply").last
        reply.click()
        res["truncated_line"] = "the chart shows only those rows" in page.locator("#report").inner_text()
        if shots: page.screenshot(path=f"{shots}/report_{key}.png")
        ask("show split names as a bar chart")
        reply = page.locator(".reply").last
        res["split_pressed"] = reply.locator('.viewseg button[aria-pressed="true"]').inner_text()
        res["split_marks"] = reply.locator(".mark").count()
        res["split_first_label"] = reply.locator(".mark").first.get_attribute("aria-label")
        res["split_svg_label"] = reply.locator("svg.svg").get_attribute("aria-label")
        ask("show the hire year of each employee")
        reply = page.locator(".reply").last
        res["hire_pressed"] = reply.locator('.viewseg button[aria-pressed="true"]').inner_text()
        res["hire_disabled"] = [reply.locator('.viewseg button', has_text=t).is_disabled() for t in ("Bar", "Pie", "Line")]
        res["hire_group_rows"] = reply.locator(".gv-row").count()
        reply.locator('.viewseg button', has_text="Table").click()
        res["hire_table_rows"] = reply.locator("tbody tr").count()
        res["hire_note"] = reply.locator(".note").first.inner_text()
        res["hire_title"] = reply.locator('.viewseg button', has_text="Bar").get_attribute("title")
        res["limits_line"] = page.locator("#limits").inner_text()
        res["count_before_errors"] = page.locator("#count").inner_text()
        def ask_plain(q):
            page.fill("#q", q)
            page.press("#q", "Enter")
            page.wait_for_selector(".reply:last-of-type .pill, .reply:last-of-type p.plain")
        for key_name, text in (("quota", "quota test"), ("unavailable", "unavailable test"), ("rate", "rate limit test")):
            ask_plain(text)
            reply = page.locator(".reply").last
            res[key_name + "_pill"] = reply.locator(".pill").first.inner_text()
            res[key_name + "_text"] = reply.locator("p.plain").inner_text()
            res[key_name + "_buttons"] = reply.locator("button").count()
            res[key_name + "_button_text"] = reply.locator("button").first.inner_text() if reply.locator("button").count() else ""
        res["count_after_errors"] = page.locator("#count").inner_text()
        # one click on Try again sends exactly one new question
        before = page.locator(".bubble").count()
        page.locator(".reply").nth(-2).locator("button", has_text="Try again").click()
        page.wait_for_function("document.querySelectorAll('.bubble').length > %d" % before)
        res["retry_adds_one"] = page.locator(".bubble").count() - before
        ask("two part pie chart")
        reply = page.locator(".reply").last
        res["pie2_disabled"] = reply.locator('.viewseg button', has_text="Pie").is_disabled()
        res["pie2_title"] = reply.locator('.viewseg button', has_text="Pie").get_attribute("title")
        res["pie2_view"] = reply.locator('.viewseg button[aria-pressed="true"]').inner_text()
        ask("groups fullname please")
        reply = page.locator(".reply").last
        res["g1_pressed"] = reply.locator('.viewseg button[aria-pressed="true"]').inner_text()
        res["g1_rows"] = reply.locator(".gv-row").count()
        res["g1_labels"] = [e.inner_text() for e in reply.locator(".gv-label").all()]
        res["g1_blocks"] = [[b.inner_text() for b in row.locator(".gv-block").all()] for row in reply.locator(".gv-row").all()]
        res["g1_counts"] = [e.inner_text() for e in reply.locator(".gv-count").all()]
        res["g1_title"] = reply.locator(".gv-title").inner_text()
        res["g1_aria"] = reply.locator(".gv").get_attribute("aria-label")
        res["g1_note"] = reply.locator(".gv-note").inner_text()
        res["g1_block_title"] = reply.locator(".gv-block").first.get_attribute("title")
        res["g1_buttons"] = [b.inner_text() for b in reply.locator(".viewseg button").all()]
        res["g1_chart_disabled"] = [reply.locator('.viewseg button', has_text=t).is_disabled() for t in ("Bar", "Pie", "Line")]
        res["g1_groups_enabled"] = not reply.locator('.viewseg button', has_text="Groups").is_disabled()
        res["g1_overflow"] = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        reply.locator('.viewseg button', has_text="Table").click()
        res["g1_table_rows"] = reply.locator("tbody tr").count()
        res["g1_groups_after_table"] = reply.locator(".gv-row").count()
        reply.locator('.viewseg button', has_text="Groups").click()
        res["g1_back_to_groups"] = reply.locator(".gv-row").count()
        reply.click()
        res["g1_report"] = page.locator("#report").inner_text()
        ask("groups split please")
        reply = page.locator(".reply").last
        res["g2_pressed"] = reply.locator('.viewseg button[aria-pressed="true"]').inner_text()
        res["g2_title"] = reply.locator(".gv-title").inner_text()
        res["g2_first_blocks"] = [b.inner_text() for b in reply.locator(".gv-row").first.locator(".gv-block").all()]
        ask("groups count as a bar chart")
        reply = page.locator(".reply").last
        res["g3_pressed"] = reply.locator('.viewseg button[aria-pressed="true"]').inner_text()
        res["g3_groups_disabled"] = reply.locator('.viewseg button', has_text="Groups").is_disabled()
        res["g3_groups_title"] = reply.locator('.viewseg button', has_text="Groups").get_attribute("title")
        res["g3_marks"] = reply.locator(".mark").count()
        ask("groups many rows")
        reply = page.locator(".reply").last
        res["g4_pressed"] = reply.locator('.viewseg button[aria-pressed="true"]').inner_text()
        res["g4_groups_disabled"] = reply.locator('.viewseg button', has_text="Groups").is_disabled()
        res["g4_groups_title"] = reply.locator('.viewseg button', has_text="Groups").get_attribute("title")
        res["g4_table_rows"] = reply.locator("tbody tr").count()
        res["g_overflow_end"] = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        ask("What is this database about?")
        reply = page.locator(".reply").last
        res["about_paragraphs"] = reply.locator("p.paragraph").count()
        res["about_tables"] = reply.locator("table").count()
        res["about_text_ok"] = reply.locator("p.paragraph").inner_text() == LONG
        res["about_chart_disabled"] = [reply.locator('.viewseg button', has_text=t).is_disabled() for t in ("Bar", "Pie", "Line")]
        page.click("#examples summary")
        page.locator("#groups .chip", has_text="About this database").click()
        page.wait_for_selector(".reply:last-of-type p.paragraph")
        reply = page.locator(".reply").last
        res["about_chip_label"] = reply.locator(".pill").first.inner_text()
        res["about_chip_paragraph"] = reply.locator("p.paragraph").inner_text() == LONG
        res["about_chip_view_control"] = reply.locator(".viewseg").count()
        reply.click()
        res["about_chip_report"] = page.locator("#report").inner_text()
        res["scroll_width"] = page.evaluate("document.documentElement.scrollWidth")
        res["inner_width"] = page.evaluate("window.innerWidth")
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
    INTERPRETER is None, reason="no interpreter with playwright importable; the browser smoke test is skipped"
)


@pytest.fixture(scope="module")
def results(tmp_path_factory, db_path):
    folder = tmp_path_factory.mktemp("browser")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(app, "DB_PATH", db_path)
        with TestClient(app.app) as client:
            response = client.get("/")
    (folder / "payload.json").write_text(
        json.dumps({"html": response.text, "csp": response.headers["Content-Security-Policy"]})
    )
    (folder / "smoke.py").write_text(SCRIPT)
    done = subprocess.run(
        [INTERPRETER, str(folder / "smoke.py"), str(folder / "payload.json")],
        capture_output=True,
        text=True,
        timeout=240,
    )
    if done.returncode == 3 and "BROWSER_UNAVAILABLE" in done.stdout:
        pytest.skip("headless browser is not installed: " + done.stdout.strip())
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


SCENES = ["light1280", "dark1280", "light420"]


def test_no_console_or_page_errors_and_the_csp_is_respected(results):
    # The stubbed rate limit answer is a real HTTP 429, which the browser logs once
    # per scene; that message is expected and nothing else may appear.
    expected = "Failed to load resource: the server responded with a status of 429 (Too Many Requests)"
    assert [e for e in results["errors"] if e != expected] == []
    assert results["errors"].count(expected) == len(SCENES)


@pytest.mark.parametrize("scene", SCENES)
def test_charts_chips_and_the_asked_views(results, scene):
    r = results[scene]
    assert r["group_names"] == ["TRY A QUESTION", "CHARTS", "BUILT IN"]
    assert r["chips"] == [
        "Draw a pie chart of orders by shipper",
        "Show total revenue by year as a line chart",
        "Top 5 customers by revenue as a bar chart",
    ]
    assert r["pie_pressed"] == "Pie" and r["pie_slices"] == 3 and r["pie_legend"] == 3
    assert r["line_pressed"] == "Line" and r["line_polyline"] == 1
    assert r["bar_pressed"] == "Bar"


@pytest.mark.parametrize("scene", SCENES)
def test_each_view_renders_and_the_table_keeps_the_values(results, scene):
    r = results[scene]
    assert r["bar_marks"] == 3
    assert r["table_rows"] == 3
    assert r["line_disabled_on_3"] is True


@pytest.mark.parametrize("scene", SCENES)
def test_keyboard_reading_of_a_bar_and_the_line(results, scene):
    r = results[scene]
    assert r["bar_mark_tabindex"] == "0" and r["bar_tip_on_focus"] is True
    assert r["bar_aria"] == "B's Beverages: 6,154,115.34"
    assert r["tip_after_focus"] is True and r["arrow_moves"] is True
    assert r["tip_text"] == "69,884,120.05\n2022"


@pytest.mark.parametrize("scene", SCENES)
def test_fallbacks_and_disabled_views(results, scene):
    r = results[scene]
    assert r["cat_pressed"] == "Bar" and r["cat_marks"] == 8 and r["cat_pie_disabled"] is True
    assert r["cat_note"] == "Pie needs 3 to 6 parts, all above zero; this has 8. Showing a bar chart instead."
    assert r["one_row_disabled"] == [True, True, True]
    assert r["one_row_title"] == "One row: the number is the answer, nothing to chart."
    # Two label columns and one number allow Bar; two rows are too few for a Pie (3 to 6 parts) and Line needs date-like labels.
    assert r["three_col_disabled"] == [False, True, True] and r["three_col_table_enabled"] is True


@pytest.mark.parametrize("scene", SCENES)
def test_report_section_and_truncation_line(results, scene):
    r = results[scene]
    assert r["report_has_chart_views"] == 1
    assert r["truncated_line"] is True


@pytest.mark.parametrize("scene", SCENES)
def test_a_long_single_text_cell_is_a_paragraph_and_the_about_chip_is_built_in(results, scene):
    r = results[scene]
    assert r["about_paragraphs"] == 1 and r["about_tables"] == 0 and r["about_text_ok"] is True
    assert r["about_chart_disabled"] == [True, True, True]
    assert r["about_chip_label"] == "Built in, no model call"
    assert r["about_chip_paragraph"] is True
    assert r["about_chip_view_control"] == 0
    assert "No SQL ran for this entry." in r["about_chip_report"]


HIRE_REASON = (
    "Charts need a number column to measure. HireYear is treated as a label. "
    "Ask for a count or total, for example: How many employees were hired each year?"
)


@pytest.mark.parametrize("scene", SCENES)
def test_split_names_draw_a_bar_with_joined_labels(results, scene):
    r = results[scene]
    assert r["split_pressed"] == "Bar" and r["split_marks"] == 3
    assert r["split_first_label"] == "Margaret Peacock: 1,908"
    assert r["split_svg_label"] == "Bar chart of OrderCount by FirstName / LastName"


@pytest.mark.parametrize("scene", SCENES)
def test_a_hire_year_column_keeps_the_helpful_reason_and_the_table(results, scene):
    r = results[scene]
    # A list without a number now opens as groups; the table is one click away.
    assert r["hire_pressed"] == "Groups" and r["hire_group_rows"] == 3
    assert r["hire_disabled"] == [True, True, True]
    assert r["hire_table_rows"] == 3
    assert r["hire_note"] == HIRE_REASON
    assert r["hire_title"] == HIRE_REASON


QUOTA_TEXT = "The free model quota is used up for now. Please try again in a minute."
BUSY_TEXT = "The model service is busy or slow right now. Please try again."


@pytest.mark.parametrize("scene", SCENES)
def test_quota_unavailable_and_rate_limited_answers(results, scene):
    r = results[scene]
    assert r["quota_pill"] == "Quota used" and r["quota_text"] == QUOTA_TEXT and r["quota_buttons"] == 0
    assert r["unavailable_pill"] == "Busy" and r["unavailable_text"] == BUSY_TEXT
    assert r["unavailable_buttons"] == 1 and r["unavailable_button_text"] == "Try again"
    assert r["rate_pill"] == "Slow down"
    assert r["rate_text"] == "You are asking too fast. Please wait 7 seconds."
    assert r["rate_buttons"] == 0


@pytest.mark.parametrize("scene", SCENES)
def test_the_three_outcomes_are_not_remembered_and_retry_sends_once(results, scene):
    r = results[scene]
    assert r["count_after_errors"] == r["count_before_errors"]
    assert r["retry_adds_one"] == 1


@pytest.mark.parametrize("scene", SCENES)
def test_the_limits_line_shows_the_rate(results, scene):
    assert "10 questions per minute" in results[scene]["limits_line"]


@pytest.mark.parametrize("scene", SCENES)
def test_a_two_part_result_cannot_be_a_pie(results, scene):
    r = results[scene]
    assert r["pie2_disabled"] is True
    assert r["pie2_title"] == "Pie needs 3 to 6 parts, all above zero; this has 2."
    # The question asked for a pie, which is refused, so the page falls back to a bar chart.
    assert r["pie2_view"] == "Bar"


STAFF_BLOCKS = [
    ["Andrew Fuller", "Janet Leverling", "Nancy Davolio"],
    ["Margaret Peacock", "Michael Suyama", "Steven Buchanan"],
    ["Anne Dodsworth", "Laura Callahan", "Robert King"],
]


@pytest.mark.parametrize("scene", SCENES)
def test_a_fullname_and_text_year_list_opens_in_groups_with_the_exact_blocks(results, scene):
    r = results[scene]
    assert r["g1_pressed"] == "Groups"
    assert r["g1_rows"] == 3 and r["g1_labels"] == ["2012", "2013", "2014"]
    assert r["g1_blocks"] == STAFF_BLOCKS
    assert r["g1_counts"] == ["3", "3", "3"]
    assert r["g1_title"] == "fullname grouped by hire_year (9 rows, 3 groups)"
    assert r["g1_aria"] == "Groups of fullname by hire_year"
    assert r["g1_note"] == "One block per row. The page draws no number the SQL did not return."
    assert r["g1_block_title"] == "Andrew Fuller"
    assert r["g1_buttons"] == ["Table", "Bar", "Pie", "Line", "Groups"]
    assert r["g1_chart_disabled"] == [True, True, True] and r["g1_groups_enabled"] is True


@pytest.mark.parametrize("scene", SCENES)
def test_the_table_button_always_works_and_groups_can_be_reopened(results, scene):
    r = results[scene]
    assert r["g1_table_rows"] == 9 and r["g1_groups_after_table"] == 0
    assert r["g1_back_to_groups"] == 3


@pytest.mark.parametrize("scene", SCENES)
def test_split_names_join_into_the_blocks_under_the_year(results, scene):
    r = results[scene]
    assert r["g2_pressed"] == "Groups"
    assert r["g2_title"] == "FirstName / LastName grouped by HireYear (9 rows, 3 groups)"
    assert r["g2_first_blocks"] == STAFF_BLOCKS[0]


@pytest.mark.parametrize("scene", SCENES)
def test_a_label_and_a_count_still_opens_in_bar_and_groups_is_refused(results, scene):
    r = results[scene]
    assert r["g3_pressed"] == "Bar" and r["g3_marks"] == 3
    assert r["g3_groups_disabled"] is True
    assert r["g3_groups_title"] == "Groups is for lists without a number. Use Bar, Pie or Line."


@pytest.mark.parametrize("scene", SCENES)
def test_ninety_three_rows_keep_groups_disabled_and_open_the_table(results, scene):
    r = results[scene]
    assert r["g4_pressed"] == "Table" and r["g4_table_rows"] == 93
    assert r["g4_groups_disabled"] is True
    assert r["g4_groups_title"] == "Groups needs 2 to 60 rows; this has 93."


@pytest.mark.parametrize("scene", SCENES)
def test_the_chart_views_section_lists_groups_and_nothing_overflows_sideways(results, scene):
    r = results[scene]
    report = r["g1_report"].lower()
    assert "groups" in report and "available" in report
    assert r["g1_overflow"] <= 0 and r["g_overflow_end"] <= 0


def test_the_theme_buttons_switch_and_the_narrow_page_does_not_scroll_sideways(results):
    assert results["light1280"]["theme_attr"] == "light"
    assert results["dark1280"]["theme_attr"] == "dark"
    narrow = results["light420"]
    assert narrow["scroll_width"] <= narrow["inner_width"]
