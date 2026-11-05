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

def answer(q, sql, cols):
    return {"question": q, "sql": sql, "columns": cols, "rows": [["secretvalue", 1]], "truncated": False, "elapsed_s": 0.1, "query_s": 0.02,
            "error": None, "error_kind": None, "source": "model", "limits": {"timeout_s": 5.0, "max_rows": 500}}

errors, out = [], {}
with sync_playwright() as p:
    try:
        browser = p.chromium.launch()
    except Exception as exc:
        print("BROWSER_UNAVAILABLE " + str(exc).splitlines()[0])
        sys.exit(3)
    for mode, width in (("six", 1280), ("six", 420), ("fetch_fails", 1280)):
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
                if mode == "fetch_fails":
                    route.fulfill(status=500, body="")
                else:
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({"timeout_s": 5.0, "max_rows": 500, "slots": 1, "memory_turns": 6, "rate_limit_per_minute": 10}))
            elif path == "/api/sandbox-demos":
                route.fulfill(status=200, content_type="application/json", body="[]")
            elif path == "/api/ask":
                body = json.loads(req.post_data)
                asks.append(body)
                q = body["question"]
                if q.startswith("long"):
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(answer(q, "SELECT " + "x" * 1990, ["c"])))
                else:
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(answer(q, "SELECT '" + q + "'", ["name", "n"])))
            else:
                route.fulfill(status=404, body="")
        page.route("http://app.test/**", handler)
        page.goto("http://app.test/")
        key = mode + str(width)
        res = {}
        def ask(q):
            n = page.locator(".reply").count()
            page.fill("#q", q)
            page.press("#q", "Enter")
            for _ in range(200):
                if page.locator(".reply").count() > n and page.locator(".reply").nth(n).locator(".chev").count():
                    break
                page.wait_for_timeout(25)
        page.wait_for_timeout(300)
        res["initial_count"] = page.locator("#count").inner_text()
        res["hint"] = page.locator("#memoryhint").inner_text()
        res["limits_line"] = page.locator("#limits").inner_text()
        counts = []
        for i in range(1, 9):
            ask("question %d" % i)
            counts.append(page.locator("#count").inner_text())
        res["counts"] = counts
        res["history_lengths"] = [len(a["history"]) for a in asks]
        res["history_for_7"] = [t["question"] for t in asks[6]["history"]]
        res["history_for_8"] = [t["question"] for t in asks[7]["history"]]
        res["no_rows_in_any_request"] = not any("secretvalue" in json.dumps(a) for a in asks)
        res["divider"] = page.locator(".divider").count()
        res["old_cards"] = page.locator(".reply.old").count()
        page.click("#newsess")
        res["after_new_session"] = page.locator("#count").inner_text()
        asks.clear()
        ask("after reset")
        res["history_after_reset"] = len(asks[-1]["history"])
        # long turns: the page cuts the oldest first
        page.click("#newsess")
        asks.clear()
        for i in range(1, 7):
            ask("long question %d" % i)
        res["long_history_questions"] = [[t["question"] for t in a["history"]] for a in asks]
        res["long_history_chars"] = [sum(len(t["question"]) + len(t["sql"]) + sum(len(c) for c in t["columns"]) for t in a["history"]) for a in asks]
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
    INTERPRETER is None, reason="no interpreter with playwright importable; the memory browser test is skipped"
)

SCENES = ["six1280", "six420", "fetch_fails1280"]


@pytest.fixture(scope="module")
def results(tmp_path_factory, db_path):
    folder = tmp_path_factory.mktemp("memory_browser")
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
        timeout=300,
    )
    if done.returncode == 3 and "BROWSER_UNAVAILABLE" in done.stdout:
        pytest.skip("headless browser is not installed: " + done.stdout.strip())
    assert done.returncode == 0, done.stderr[-2500:]
    return json.loads(done.stdout)


def test_no_console_or_page_errors(results):
    assert results["errors"] == []


@pytest.mark.parametrize("scene", SCENES)
def test_the_texts_say_six_from_the_start_and_when_the_limits_fetch_fails(results, scene):
    r = results[scene]
    assert r["initial_count"] == "Memory: 0 of 6 questions"
    assert "The last 6 questions are remembered" in r["hint"]
    assert "memory: last 6 questions, rows never sent" in r["limits_line"]


@pytest.mark.parametrize("scene", SCENES)
def test_the_counter_counts_up_to_six_and_stays_there(results, scene):
    r = results[scene]
    assert r["counts"] == [f"Memory: {n} of 6 questions" for n in (1, 2, 3, 4, 5, 6, 6, 6)]
    assert r["after_new_session"] == "Memory: 0 of 6 questions"
    assert r["history_after_reset"] == 0


@pytest.mark.parametrize("scene", SCENES)
def test_each_request_carries_at_most_the_last_six_turns_and_drops_the_oldest(results, scene):
    r = results[scene]
    assert r["history_lengths"] == [0, 1, 2, 3, 4, 5, 6, 6]
    assert r["history_for_7"] == [f"question {i}" for i in range(1, 7)]
    assert r["history_for_8"] == [f"question {i}" for i in range(2, 8)]
    assert r["divider"] == 1 and r["old_cards"] >= 2


@pytest.mark.parametrize("scene", SCENES)
def test_row_values_are_never_sent(results, scene):
    assert results[scene]["no_rows_in_any_request"] is True


@pytest.mark.parametrize("scene", SCENES)
def test_long_turns_make_the_page_drop_the_oldest_and_keep_the_newest(results, scene):
    r = results[scene]
    questions = r["long_history_questions"]
    assert questions[0] == [] and questions[1] == ["long question 1"]
    for n, sent in enumerate(questions, start=1):
        assert len(sent) <= 6
        if sent:
            assert sent[-1] == f"long question {n - 1}"
            assert sent == [f"long question {i}" for i in range(n - len(sent), n)]
    assert all(chars <= 9000 for chars in r["long_history_chars"])
    # six long turns do not fit in 9000 characters, so the sixth request lost at least one old turn
    assert len(questions[5]) < 5 or r["long_history_chars"][5] <= 9000
    assert len(questions[5]) <= 4
