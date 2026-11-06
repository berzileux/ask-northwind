import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import app
from model import MODEL_NAME

# The page is loaded in a real headless browser with every request answered by a stub, so no
# server and no model are involved. The name in the stubbed limits is the real constant.
SCRIPT = r"""
import json, sys
from playwright.sync_api import sync_playwright

payload = json.load(open(sys.argv[1]))
NAME = payload["name"]
LIMITS = {"timeout_s": 5.0, "max_rows": 500, "slots": 1, "memory_turns": 6, "rate_limit_per_minute": 10, "model": NAME}

def body_for(q):
    base = {"question": q, "sql": "", "columns": [], "rows": [], "truncated": False, "elapsed_s": 0.0, "query_s": None,
            "error": "x", "error_kind": None, "source": "model", "limits": {"timeout_s": 5.0, "max_rows": 500}}
    for kind in ("quota", "unavailable", "rejected", "timeout"):
        if q.startswith(kind):
            base["error_kind"] = kind
            return base
    base.update({"sql": "SELECT 1", "columns": ["name", "n"], "rows": [["a", 5], ["b", 3]], "error": None})
    return base

errors, out = [], {}
with sync_playwright() as p:
    try:
        browser = p.chromium.launch()
    except Exception as exc:
        print("BROWSER_UNAVAILABLE " + str(exc).splitlines()[0])
        sys.exit(3)
    for scheme, width, mode in (("light", 1280, "ok"), ("dark", 1280, "ok"), ("light", 420, "ok"), ("light", 1280, "fails"), ("light", 1280, "empty")):
        ctx = browser.new_context(viewport={"width": width, "height": 900})
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
        asks = []
        def handler(route):
            req = route.request
            path = req.url.split("app.test", 1)[1]
            if path == "/":
                route.fulfill(status=200, content_type="text/html", body=payload["html"], headers={"Content-Security-Policy": payload["csp"]})
            elif path == "/api/limits":
                if mode == "fails":
                    route.fulfill(status=500, body="")
                elif mode == "empty":
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(dict(LIMITS, model="  ")))
                else:
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(LIMITS))
            elif path == "/api/sandbox-demos":
                route.fulfill(status=200, content_type="application/json", body=json.dumps([{"id": "delete", "label": "Delete demo"}]))
            elif path == "/api/sandbox-demo":
                # A demo answer that looks like a quota failure must still leave the dot alone.
                route.fulfill(status=200, content_type="application/json", body=json.dumps(body_for("quota demo")))
            elif path == "/api/schema":
                route.fulfill(status=200, content_type="application/json", body=json.dumps(payload["schema"]))
            elif path == "/api/about":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"text": "About text."}))
            elif path == "/api/ask":
                body = json.loads(req.post_data)
                asks.append(body)
                q = body["question"]
                if q.startswith("ratelimited"):
                    route.fulfill(status=429, content_type="application/json", headers={"Retry-After": "7"},
                                  body=json.dumps(dict(body_for(q), error_kind="rate_limited")))
                else:
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(body_for(q)))
            else:
                route.fulfill(status=404, body="")
        page.route("http://app.test/**", handler)
        page.goto("http://app.test/")
        page.click('.seg button[data-t="%s"]' % scheme)
        page.wait_for_timeout(300)
        key = scheme + str(width) + mode
        res = {}
        pill = page.locator("#modelpill")
        def dot():
            return {"stopped": "stopped" in (pill.get_attribute("class") or ""), "title": pill.get_attribute("title")}
        def ask(q):
            n = page.locator(".reply").count()
            page.fill("#q", q)
            page.press("#q", "Enter")
            for _ in range(200):
                if page.locator(".reply").count() > n and page.locator(".reply").nth(n).locator(".working").count() == 0:
                    break
                page.wait_for_timeout(25)
            page.wait_for_timeout(50)
            return dot()
        res["visible"] = pill.is_visible()
        if mode == "ok":
            res["text"] = page.locator("#modelpill .mname").inner_text()
            res["aria"] = pill.get_attribute("aria-label")
            res["dot_hidden"] = page.locator("#modelpill .mdot").get_attribute("aria-hidden")
            res["start"] = dot()
            box = pill.bounding_box()
            res["pill_inside"] = box["x"] >= 0 and box["x"] + box["width"] <= width
            res["pill_below_subtitle"] = box["y"] + box["height"] <= page.locator(".sub").bounding_box()["y"] + 1
            res["font"] = pill.evaluate("e => getComputedStyle(e).fontFamily")
            seq = {}
            seq["quota"] = ask("quota one")
            seq["ok1"] = ask("fine one")
            seq["unavailable"] = ask("unavailable one")
            seq["rejected_while_stopped"] = ask("rejected one")
            seq["timeout_while_stopped"] = ask("timeout one")
            seq["ratelimited_while_stopped"] = ask("ratelimited one")
            seq["ok2"] = ask("fine two")
            seq["rejected_while_normal"] = ask("rejected two")
            seq["timeout_while_normal"] = ask("timeout two")
            seq["ratelimited_while_normal"] = ask("ratelimited two")
            res["seq"] = seq
            count_before = page.locator("#count").inner_text()
            res["count_before_builtins"] = count_before
            # built-in entries, first while normal then while stopped
            def chip(label):
                if page.locator("#examples").get_attribute("open") is None:
                    page.click("#examples summary")
                page.locator("#groups .chip", has_text=label).click()
            def builtins():
                found = {}
                chip("Database map")
                page.locator(".dg-svg").last.wait_for()
                found["map"] = dot()
                chip("About this database")
                page.wait_for_timeout(300)
                found["about"] = dot()
                chip("Delete demo")
                page.wait_for_timeout(300)
                found["demo"] = dot()
                page.fill("#q", "please draw a diagram")
                page.press("#q", "Enter")
                page.wait_for_timeout(300)
                found["diagram"] = dot()
                return found
            res["builtins_normal"] = builtins()
            ask("quota two")
            res["builtins_stopped"] = builtins()
            res["count_after_builtins"] = page.locator("#count").inner_text()
            res["requests_hold_no_name"] = not any(NAME in json.dumps(a) for a in asks)
            res["history_keys"] = sorted({k for a in asks for t in a["history"] for k in t})
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
    INTERPRETER is None, reason="no interpreter with playwright importable; the model pill browser test is skipped"
)

SCENES = ["light1280ok", "dark1280ok", "light420ok"]
NORMAL = {"stopped": False, "title": "Model: " + MODEL_NAME}
QUOTA = {"stopped": True, "title": "Last request failed: quota used"}
BUSY = {"stopped": True, "title": "Last request failed: model busy"}


@pytest.fixture(scope="module")
def results(tmp_path_factory, db_path):
    folder = tmp_path_factory.mktemp("model_pill_browser")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(app, "DB_PATH", db_path)
        with TestClient(app.app) as client:
            response = client.get("/")
            schema = client.get("/api/schema").json()
    (folder / "payload.json").write_text(
        json.dumps({"html": response.text, "csp": response.headers["Content-Security-Policy"], "schema": schema, "name": MODEL_NAME})
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
    return json.loads(done.stdout)


def test_no_console_or_page_errors(results):
    assert results["errors"] == []


@pytest.mark.parametrize("scene", SCENES)
def test_the_pill_shows_the_exact_name_with_a_label_and_a_hidden_dot(results, scene):
    r = results[scene]
    assert r["visible"] is True
    assert r["text"] == MODEL_NAME
    assert r["aria"] == "Model: " + MODEL_NAME
    assert r["dot_hidden"] == "true"
    assert "monospace" in r["font"].lower() or "mono" in r["font"].lower() or "menlo" in r["font"].lower() or "consolas" in r["font"].lower()


@pytest.mark.parametrize("scene", SCENES)
def test_the_pill_sits_above_the_subtitle_and_inside_the_screen(results, scene):
    r = results[scene]
    assert r["pill_inside"] is True and r["pill_below_subtitle"] is True
    assert r["overflow"] <= 0


@pytest.mark.parametrize("scene", ["light1280fails", "light1280empty"])
def test_no_pill_when_the_limits_fail_or_the_name_is_blank(results, scene):
    assert results[scene]["visible"] is False


@pytest.mark.parametrize("scene", SCENES)
def test_the_dot_is_normal_at_load(results, scene):
    assert results[scene]["start"] == NORMAL


@pytest.mark.parametrize("scene", SCENES)
def test_quota_and_unavailable_stop_the_dot_and_a_success_restores_it(results, scene):
    seq = results[scene]["seq"]
    assert seq["quota"] == QUOTA
    assert seq["ok1"] == NORMAL
    assert seq["unavailable"] == BUSY
    assert seq["ok2"] == NORMAL


@pytest.mark.parametrize("scene", SCENES)
def test_rate_limited_rejected_and_timeout_leave_the_dot_unchanged(results, scene):
    seq = results[scene]["seq"]
    assert seq["rejected_while_stopped"] == BUSY
    assert seq["timeout_while_stopped"] == BUSY
    assert seq["ratelimited_while_stopped"] == BUSY
    assert seq["rejected_while_normal"] == NORMAL
    assert seq["timeout_while_normal"] == NORMAL
    assert seq["ratelimited_while_normal"] == NORMAL


@pytest.mark.parametrize("scene", SCENES)
def test_built_in_entries_leave_the_dot_unchanged(results, scene):
    r = results[scene]
    assert all(v == NORMAL for v in r["builtins_normal"].values())
    assert all(v == QUOTA for v in r["builtins_stopped"].values())
    assert set(r["builtins_normal"]) == {"map", "about", "demo", "diagram"}


@pytest.mark.parametrize("scene", SCENES)
def test_the_pill_is_not_part_of_the_session_memory(results, scene):
    r = results[scene]
    assert r["count_after_builtins"] == r["count_before_builtins"]
    assert r["requests_hold_no_name"] is True
    assert set(r["history_keys"]) <= {"question", "sql", "columns", "row_count", "outcome"}
