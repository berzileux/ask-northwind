import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import app

# The page is loaded in a real headless browser with every request answered by a stub and
# every request to /api/ask recorded, so no server and no model are involved.
SCRIPT = r"""
import json, sys
from playwright.sync_api import sync_playwright

payload = json.load(open(sys.argv[1]))
folder = sys.argv[2]
NOTE = "Shown from the built-in Database map because the question asked for a diagram."

def answer(q, cols, rows):
    return {"question": q, "sql": "SELECT 1", "columns": cols, "rows": rows, "truncated": False, "elapsed_s": 0.1, "query_s": 0.02,
            "error": None, "error_kind": None, "source": "model", "limits": {"timeout_s": 5.0, "max_rows": 500}}

def stub(q):
    if "quota" in q:
        return {"question": q, "sql": "", "columns": [], "rows": [], "truncated": False, "elapsed_s": 0.0, "query_s": None,
                "error": "The free model quota is used up for now. Please try again in a minute.", "error_kind": "quota", "source": "model",
                "limits": {"timeout_s": 5.0, "max_rows": 500}}
    return answer(q, ["name", "n"], [["a", 5], ["b", 3], ["c", 8]])

errors, out = [], {}
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
        asks = []
        paths = []
        state = {"quota": False}
        def handler(route):
            req = route.request
            path = req.url.split("app.test", 1)[1]
            paths.append(path)
            if path == "/":
                route.fulfill(status=200, content_type="text/html", body=payload["html"], headers={"Content-Security-Policy": payload["csp"]})
            elif path == "/api/limits":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"timeout_s": 5.0, "max_rows": 500, "slots": 1, "memory_turns": 4, "rate_limit_per_minute": 10}))
            elif path == "/api/sandbox-demos":
                route.fulfill(status=200, content_type="application/json", body="[]")
            elif path == "/api/schema":
                route.fulfill(status=200, content_type="application/json", body=json.dumps(payload["schema"]))
            elif path == "/api/ask":
                body = json.loads(req.post_data)
                asks.append(body)
                if state["quota"]:
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(stub("quota")))
                else:
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(stub(body["question"])))
            else:
                route.fulfill(status=404, body="")
        page.route("http://app.test/**", handler)
        page.goto("http://app.test/")
        page.click('.seg button[data-t="%s"]' % scheme)
        key = scheme + str(width)
        res = {}
        def type_and_send(q):
            page.fill("#q", q)
            page.press("#q", "Enter")
        def wait_cards(n):
            # Polled from here: the page policy forbids evaluating strings, so no wait_for_function.
            for _ in range(200):
                if page.locator(".reply").count() >= n:
                    return
                page.wait_for_timeout(50)
            raise RuntimeError("card did not appear")
        def last():
            return page.locator(".reply").nth(page.locator(".reply").count() - 1)

        # a normal question first, so there is memory to protect
        type_and_send("first normal question")
        page.wait_for_selector(".reply:last-of-type .chev")
        res["count_before"] = page.locator("#count").inner_text()
        asks_before = len(asks)

        # the typed diagram request
        type_and_send("can draw me a diagram from this")
        page.wait_for_selector(".dg-svg")
        card = last()
        res["asks_after_diagram"] = len(asks) - asks_before
        res["bubbles"] = page.locator(".bubble").all_inner_texts()
        res["input_cleared"] = page.input_value("#q")
        res["diagram_boxes"] = card.locator(".dg-box").count()
        res["diagram_pressed"] = card.locator('.viewseg button[aria-pressed="true"]').inner_text()
        res["pill"] = card.locator(".pill").first.inner_text()
        res["note_lines"] = [e.inner_text() for e in card.locator(".note").all() if e.inner_text()]
        res["chevron"] = card.locator(".chev").count()
        res["save_button"] = card.locator("button", has_text="Save as image").count()
        res["count_after"] = page.locator("#count").inner_text()
        res["overflow"] = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        res["ask_paths"] = [x for x in paths if x == "/api/ask"].__len__()

        # collapse and the PNG
        card.locator(".chev").click()
        res["collapsed"] = card.evaluate("e => e.classList.contains('collapsed')")
        res["collapsed_line"] = card.locator(".qline").inner_text()
        card.locator(".chev").click()
        with page.expect_download() as info:
            card.locator("button", has_text="Save as image").click()
        dl = info.value
        target = folder + "/" + key + "_" + dl.suggested_filename
        dl.save_as(target)
        data = open(target, "rb").read()
        res["png"] = [dl.suggested_filename, data[:8].hex(), len(data)]

        # the next normal question carries the same history as before
        type_and_send("second normal question")
        wait_cards(3)
        page.wait_for_timeout(300)
        res["history_questions"] = [t["question"] for t in asks[-1]["history"]]

        # other phrasings and chart phrasings
        for q in ("schematic diagram on how these tables relate", "ERD please"):
            n = len(asks)
            type_and_send(q)
            page.wait_for_selector(".reply:last-of-type .dg-svg")
            res.setdefault("other_phrasings", []).append(len(asks) - n)
        for q in ("draw a pie chart of revenue by category", "show orders per year as a line chart", "list me its relationships with its tables"):
            n = len(asks)
            type_and_send(q)
            page.wait_for_timeout(400)
            res.setdefault("sent_questions", []).append([len(asks) - n, asks[-1]["question"] if len(asks) > n else None])

        # the chip has no explanatory line
        page.click("#examples summary") if not page.evaluate("document.getElementById('examples').open") else None
        n_cards = page.locator(".reply").count()
        page.locator("#groups .chip", has_text="Database map").click()
        wait_cards(n_cards + 1)
        chip_card = last()
        chip_card.locator(".dg-svg").wait_for()
        res["chip_note_lines"] = [e.inner_text() for e in chip_card.locator(".note").all() if e.inner_text()]
        res["chip_bubble"] = page.locator(".bubble").last.inner_text()
        res["chip_asks"] = len(asks)

        # the quota is used up: the model request fails but the diagram still works
        state["quota"] = True
        type_and_send("quota check normal question")
        page.wait_for_selector(".pill:has-text('Quota used')")
        n_cards = page.locator(".reply").count()
        n = len(asks)
        type_and_send("diagram of the tables while the quota is used up")
        wait_cards(n_cards + 1)
        last().locator(".dg-svg").wait_for()
        res["quota_diagram_asks"] = len(asks) - n
        res["quota_diagram_note"] = [e.inner_text() for e in last().locator(".note").all() if e.inner_text()]
        res["final_overflow"] = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
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
    INTERPRETER is None, reason="no interpreter with playwright importable; the intent browser test is skipped"
)

SCENES = ["light1280", "dark1280", "light420"]
NOTE = "Shown from the built-in Database map because the question asked for a diagram."


@pytest.fixture(scope="module")
def results(tmp_path_factory, real_db_path):
    folder = tmp_path_factory.mktemp("intent_browser")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(app, "DB_PATH", real_db_path)
        with TestClient(app.app) as client:
            response = client.get("/")
            schema = client.get("/api/schema").json()
    (folder / "payload.json").write_text(
        json.dumps({"html": response.text, "csp": response.headers["Content-Security-Policy"], "schema": schema})
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
    assert done.returncode == 0, done.stderr[-2500:]
    return json.loads(done.stdout)


def test_no_console_or_page_errors_and_the_csp_is_respected(results):
    assert results["errors"] == []


@pytest.mark.parametrize("scene", SCENES)
def test_a_typed_diagram_request_shows_the_map_with_no_request_to_ask(results, scene):
    r = results[scene]
    assert r["asks_after_diagram"] == 0
    assert r["bubbles"][-1] == "can draw me a diagram from this"
    assert r["input_cleared"] == ""
    assert r["diagram_boxes"] == 13 and r["diagram_pressed"] == "Diagram"
    assert r["pill"] == "Built in, no model call"
    assert r["note_lines"] == [NOTE]
    assert r["chevron"] == 1 and r["save_button"] == 1
    assert r["overflow"] <= 0 and r["final_overflow"] <= 0


@pytest.mark.parametrize("scene", SCENES)
def test_the_entry_is_not_remembered_and_later_requests_carry_the_same_history(results, scene):
    r = results[scene]
    assert r["count_after"] == r["count_before"] == "Memory: 1 of 4 questions"
    assert r["history_questions"] == ["first normal question"]


@pytest.mark.parametrize("scene", SCENES)
def test_the_new_entry_collapses_and_saves_the_png(results, scene):
    r = results[scene]
    assert r["collapsed"] is True and r["collapsed_line"] == "can draw me a diagram from this"
    name, head, size = r["png"]
    assert name == "northwind-database-map.png" and head == "89504e470d0a1a0a" and size > 5000


@pytest.mark.parametrize("scene", SCENES)
def test_other_diagram_phrasings_also_make_no_request(results, scene):
    assert results[scene]["other_phrasings"] == [0, 0]


@pytest.mark.parametrize("scene", SCENES)
def test_chart_and_relationship_list_questions_still_go_to_the_model(results, scene):
    sent = results[scene]["sent_questions"]
    assert sent == [
        [1, "draw a pie chart of revenue by category"],
        [1, "show orders per year as a line chart"],
        [1, "list me its relationships with its tables"],
    ]


@pytest.mark.parametrize("scene", SCENES)
def test_the_chip_entry_has_no_explanatory_line(results, scene):
    r = results[scene]
    assert r["chip_note_lines"] == []
    assert r["chip_bubble"] == "Database map"


@pytest.mark.parametrize("scene", SCENES)
def test_a_typed_diagram_request_works_while_the_quota_is_used_up(results, scene):
    r = results[scene]
    assert r["quota_diagram_asks"] == 0
    assert r["quota_diagram_note"] == [NOTE]
