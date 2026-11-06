import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import app
from model import MODEL_NAME

SUBTITLE = "Ask the Northwind sample database a question in plain English. The page shows the answer and the SQL that ran."

# The page is loaded in a real headless browser with every request answered by a stub.
SCRIPT = r"""
import json, sys
from playwright.sync_api import sync_playwright

payload = json.load(open(sys.argv[1]))
NAME = payload["name"]
LIMITS = {"timeout_s": 5.0, "max_rows": 500, "slots": 1, "memory_turns": 6, "rate_limit_per_minute": 10, "model": NAME}

def reply(q):
    base = {"question": q, "sql": "", "columns": [], "rows": [], "truncated": False, "elapsed_s": 0.0, "query_s": None,
            "error": "x", "error_kind": None, "source": "model", "limits": {"timeout_s": 5.0, "max_rows": 500}}
    if q.startswith("quota"):
        base["error_kind"] = "quota"
        return base
    base.update({"sql": "SELECT 1", "columns": ["name", "n"], "rows": [["a", 5], ["b", 3]], "error": None})
    return base

def box(loc):
    b = loc.bounding_box()
    return {"x": b["x"], "y": b["y"], "w": b["width"], "h": b["height"]}

errors, out = [], {}
SCENES = [("light", 1280, "ok"), ("light", 1100, "ok"), ("light", 900, "ok"), ("dark", 1280, "ok"), ("dark", 1100, "ok"),
          ("light", 420, "ok"), ("dark", 420, "ok"), ("light", 1280, "fails"), ("light", 420, "fails")]
with sync_playwright() as p:
    try:
        browser = p.chromium.launch()
    except Exception as exc:
        print("BROWSER_UNAVAILABLE " + str(exc).splitlines()[0])
        sys.exit(3)
    for scheme, width, mode in SCENES:
        ctx = browser.new_context(viewport={"width": width, "height": 900})
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
        def handler(route):
            req = route.request
            path = req.url.split("app.test", 1)[1]
            if path == "/":
                route.fulfill(status=200, content_type="text/html", body=payload["html"], headers={"Content-Security-Policy": payload["csp"]})
            elif path == "/api/limits":
                if mode == "fails":
                    route.fulfill(status=500, body="")
                else:
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(LIMITS))
            elif path == "/api/sandbox-demos":
                route.fulfill(status=200, content_type="application/json", body="[]")
            elif path == "/api/ask":
                body = json.loads(req.post_data)
                route.fulfill(status=200, content_type="application/json", body=json.dumps(reply(body["question"])))
            else:
                route.fulfill(status=404, body="")
        page.route("http://app.test/**", handler)
        page.goto("http://app.test/")
        page.click('.seg button[data-t="%s"]' % scheme)
        page.wait_for_timeout(300)
        res = {}
        pill = page.locator("#modelpill")
        res["pill_visible"] = pill.is_visible()
        res["h1_text"] = page.locator("h1").inner_text()
        res["sub_text"] = page.locator(".sub").inner_text()
        sub = box(page.locator(".sub"))
        res["sub_h"] = sub["h"]
        res["sub_line_height"] = float(page.locator(".sub").evaluate("e => parseFloat(getComputedStyle(e).lineHeight) || parseFloat(getComputedStyle(e).fontSize) * 1.2"))
        title = box(page.locator("h1"))
        tools = box(page.locator(".tools"))
        res["title"], res["tools"], res["sub"] = title, tools, sub
        if mode == "ok":
            pb = box(pill)
            res["pill"] = pb
            res["centre_diff"] = abs((title["y"] + title["h"] / 2) - (pb["y"] + pb["h"] / 2))
            def dot():
                return "stopped" in (pill.get_attribute("class") or "")
            res["dot_start"] = dot()
            def ask(q):
                n = page.locator(".reply").count()
                page.fill("#q", q)
                page.press("#q", "Enter")
                for _ in range(200):
                    if page.locator(".reply").count() > n and page.locator(".reply").nth(n).locator(".working").count() == 0:
                        break
                    page.wait_for_timeout(25)
                page.wait_for_timeout(50)
            ask("quota one")
            res["dot_after_quota"] = dot()
            res["title_after_quota"] = pill.get_attribute("title")
            ask("fine one")
            res["dot_after_ok"] = dot()
        res["overflow"] = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        res["theme_visible"] = page.locator('.seg button[data-t="dark"]').is_visible()
        page.click('.seg button[data-t="system"]')
        res["theme_clickable"] = page.locator('.seg button[data-t="system"]').get_attribute("aria-pressed")
        out["%s%d%s" % (scheme, width, mode)] = res
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
    INTERPRETER is None, reason="no interpreter with playwright importable; the header browser test is skipped"
)

WIDE = ["light1280ok", "light1100ok", "dark1280ok", "dark1100ok"]
ONE_LINE = WIDE + ["light900ok"]
NARROW = ["light420ok", "dark420ok", "light420fails"]


@pytest.fixture(scope="module")
def results(tmp_path_factory, db_path):
    folder = tmp_path_factory.mktemp("header_browser")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(app, "DB_PATH", db_path)
        with TestClient(app.app) as client:
            response = client.get("/")
    (folder / "payload.json").write_text(
        json.dumps({"html": response.text, "csp": response.headers["Content-Security-Policy"], "name": MODEL_NAME})
    )
    (folder / "smoke.py").write_text(SCRIPT)
    done = subprocess.run(
        [INTERPRETER, str(folder / "smoke.py"), str(folder / "payload.json")],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if done.returncode == 3 and "BROWSER_UNAVAILABLE" in done.stdout:
        pytest.skip("headless browser is not installed: " + done.stdout.strip())
    assert done.returncode == 0, done.stderr[-2500:]
    data = json.loads(done.stdout)
    print("MEASURED", json.dumps({k: {"sub_h": v["sub_h"], "lh": v["sub_line_height"], "centre_diff": v.get("centre_diff")} for k, v in data.items() if k != "errors"}))
    return data


def test_no_console_or_page_errors(results):
    assert results["errors"] == []


@pytest.mark.parametrize("scene", ONE_LINE)
def test_the_subtitle_is_one_line_and_unchanged(results, scene):
    r = results[scene]
    assert r["sub_text"] == SUBTITLE
    assert r["sub_h"] <= 1.6 * r["sub_line_height"]


@pytest.mark.parametrize("scene", WIDE)
def test_the_pill_is_beside_the_title_and_the_theme_control_is_at_the_right(results, scene):
    r = results[scene]
    assert r["h1_text"] == "ask-northwind"
    assert r["centre_diff"] <= 6
    assert r["pill"]["x"] > r["title"]["x"] + r["title"]["w"]
    assert r["tools"]["x"] > r["pill"]["x"] + r["pill"]["w"]
    assert r["sub"]["y"] >= r["title"]["y"] + r["title"]["h"]
    assert r["sub"]["w"] > r["title"]["w"] + r["pill"]["w"] + r["tools"]["w"] and r["sub"]["x"] <= r["title"]["x"] + 1


@pytest.mark.parametrize("scene", NARROW)
def test_phone_width_has_no_sideways_scroll_and_keeps_the_theme_control(results, scene):
    r = results[scene]
    assert r["overflow"] <= 0
    assert r["theme_visible"] is True and r["theme_clickable"] == "true"
    assert r["sub_text"] == SUBTITLE


@pytest.mark.parametrize("scene", ["light420ok", "dark420ok"])
def test_phone_width_still_shows_the_pill(results, scene):
    r = results[scene]
    assert r["pill_visible"] is True
    assert r["pill"]["x"] >= 0 and r["pill"]["x"] + r["pill"]["w"] <= 420


def test_no_pill_when_the_limits_fail_and_the_title_row_still_renders(results):
    for scene in ("light1280fails", "light420fails"):
        r = results[scene]
        assert r["pill_visible"] is False
        assert r["h1_text"] == "ask-northwind"
        assert r["title"]["h"] > 0 and r["theme_visible"] is True
        assert r["overflow"] <= 0
    wide = results["light1280fails"]
    assert wide["tools"]["x"] > wide["title"]["x"] + wide["title"]["w"]


@pytest.mark.parametrize("scene", WIDE + NARROW[:2])
def test_the_dot_states_still_work_in_the_new_position(results, scene):
    r = results[scene]
    assert r["dot_start"] is False
    assert r["dot_after_quota"] is True and r["title_after_quota"] == "Last request failed: quota used"
    assert r["dot_after_ok"] is False
