import json
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import app

# The page is loaded in a real headless browser with every request answered by a stub, so
# no server and no model are involved. The browser tool lives outside the project
# environment, so a separate interpreter runs the script; the file is skipped without one.
SCRIPT = r"""
import base64, json, math, re, sys
from playwright.sync_api import sync_playwright

payload = json.load(open(sys.argv[1]))
folder = sys.argv[2]

def answer(q, cols, rows):
    return {"question": q, "sql": "SELECT '" + q[:14] + "'", "columns": cols, "rows": rows, "truncated": False, "elapsed_s": 0.1, "query_s": 0.02,
            "error": None, "error_kind": None, "source": "model", "limits": {"timeout_s": 5.0, "max_rows": 500}}

LONG = "About this database. " + "This sentence makes the text longer than one hundred and twenty characters. " * 3

def stub(q):
    if "bar chart answer" in q: return answer(q, ["name", "n"], [["a", 5], ["b", 3], ["c", 8], ["d", 2], ["e", 6]])
    if "text answer" in q: return answer(q, ["name", "city"], [["Ann Lee", "Paris"], ["Bo Chen", "Rome"], ["Cy Diaz", "Oslo"]])
    return answer(q, ["name", "n"], [["a", 1], ["b", 2], ["c", 3]])

errors = []
requests = []
out = {}
HEX = lambda h: [int(h[i:i + 2], 16) for i in (1, 3, 5)]

def bezier(path, t):
    nums = [float(x) for x in path]
    x1, y1, c1x, c1y, c2x, c2y, x2, y2 = nums
    u = 1 - t
    return (u**3 * x1 + 3 * u * u * t * c1x + 3 * u * t * t * c2x + t**3 * x2,
            u**3 * y1 + 3 * u * u * t * c1y + 3 * u * t * t * c2y + t**3 * y2)

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
                route.fulfill(status=200, content_type="application/json", body=json.dumps(payload["schema"]))
            elif path == "/api/about":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"text": LONG}))
            elif path == "/api/ask":
                body = json.loads(req.post_data)
                requests.append(body)
                route.fulfill(status=200, content_type="application/json", body=json.dumps(stub(body["question"])))
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
            page.wait_for_selector(".reply:last-of-type .chev")
        def last():
            # A fixed index, so the card stays the same card after later questions are asked.
            return page.locator(".reply").nth(page.locator(".reply").count() - 1)
        def token(name):
            return page.evaluate("(n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim()", name)

        # ---- the Database map as a diagram
        page.locator("#groups .chip", has_text="Database map").click()
        page.wait_for_selector(".dg-svg")
        card = last()
        res["map_boxes"] = card.locator(".dg-box").count()
        res["map_links"] = card.locator(".dg-link").count()
        res["map_self_links"] = page.evaluate("() => Array.from(document.querySelectorAll('.dg-link path')).filter(p => { const n = p.getAttribute('d').match(/-?[0-9.]+/g).map(Number); return n[0] === n[6]; }).length")
        res["map_legend"] = card.locator(".dg-legend").inner_text()
        res["map_meta"] = card.locator(".reply-top .meta").first.inner_text()
        res["map_pill"] = card.locator(".pill").first.inner_text()
        res["map_control"] = [b.inner_text() for b in card.locator(".viewseg button").all()]
        res["map_pressed"] = card.locator('.viewseg button[aria-pressed="true"]').inner_text()
        res["map_svg_aria"] = card.locator("svg.dg-svg").get_attribute("aria-label")
        res["map_svg_role"] = card.locator("svg.dg-svg").get_attribute("role")
        res["map_links_hidden"] = card.locator(".dg-link").first.get_attribute("aria-hidden")
        res["map_min_width"] = page.evaluate("parseFloat(getComputedStyle(document.querySelector('.dg-svg')).minWidth)")
        res["map_box_tabindex"] = card.locator(".dg-box").first.get_attribute("tabindex")
        res["map_orders_aria"] = card.locator('.dg-box[aria-label^="Orders."]').get_attribute("aria-label")
        res["map_orders_title"] = card.locator('.dg-box[aria-label^="Orders."] title').text_content()
        res["map_viewbox"] = card.locator("svg.dg-svg").get_attribute("viewBox")
        res["map_scroll_inside"] = page.evaluate("(() => { const d = document.querySelector('.dg'); return { client: d.clientWidth, scroll: d.scrollWidth }; })()")
        res["map_doc_overflow"] = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        # hover and focus
        orders = card.locator('.dg-box[aria-label^="Orders."]')
        orders.hover()
        res["hover_hi_boxes"] = sorted(e.get_attribute("aria-label").split(".")[0] for e in card.locator(".dg-box.dg-hi").all())
        res["hover_dim_boxes"] = card.locator(".dg-box.dg-dim").count()
        res["hover_hi_links"] = card.locator(".dg-link.dg-hi").count()
        res["hover_dim_links"] = card.locator(".dg-link.dg-dim").count()
        page.mouse.move(2, 2)
        res["after_leave"] = [card.locator(".dg-hi").count(), card.locator(".dg-dim").count()]
        orders.focus()
        res["focus_hi_boxes"] = card.locator(".dg-box.dg-hi").count()
        page.keyboard.press("Escape")
        res["after_escape"] = [card.locator(".dg-hi").count(), card.locator(".dg-dim").count()]
        orders.focus()
        card.locator('.dg-box[aria-label^="Regions."]').focus()
        res["after_blur_then_focus_other"] = card.locator(".dg-box.dg-hi").count()
        page.evaluate("document.activeElement.blur()")
        res["after_blur"] = [card.locator(".dg-hi").count(), card.locator(".dg-dim").count()]
        # list and back
        card.locator(".viewseg button", has_text="List").click()
        res["list_items"] = card.locator("ul.checks li").count()
        res["list_visible"] = card.locator("ul.checks").is_visible()
        res["diagram_hidden_in_list"] = card.locator(".dg-svg").is_hidden()
        res["list_first"] = card.locator("ul.checks li").first.inner_text()
        card.locator(".viewseg button", has_text="Diagram").click()
        res["diagram_back"] = card.locator(".dg-svg").is_visible() and card.locator("ul.checks").is_hidden()

        # ---- the PNG, from the diagram and from the list state
        sampler = ctx.new_page()
        sampler.set_content("<canvas id=c></canvas>")
        pixel_js = (
            "async (arg) => {"
            " const img = new Image(); img.src = 'data:image/png;base64,' + arg.b64; await img.decode();"
            " const c = document.getElementById('c'); c.width = img.width; c.height = img.height;"
            " const x = c.getContext('2d'); x.drawImage(img, 0, 0);"
            " const px = (a, b) => Array.from(x.getImageData(a, b, 1, 1).data).slice(0, 3);"
            " const near = (cx, cy, color) => { let best = 999; for (let dy = -3; dy <= 3; dy++) for (let dx = -3; dx <= 3; dx++) {"
            "   const p = px(cx + dx, cy + dy); const d = Math.max(Math.abs(p[0]-color[0]), Math.abs(p[1]-color[1]), Math.abs(p[2]-color[2])); if (d < best) best = d; } return best; };"
            " return { width: img.width, height: img.height, corner: px(2, 2), box: px(arg.bx, arg.by), link: near(arg.lx, arg.ly, arg.linkColor) };"
            " }"
        )
        def save_png(tag):
            with page.expect_download() as info:
                card.locator("button", has_text="Save as image").click()
            dl = info.value
            target = folder + "/" + key + "_" + tag + "_" + dl.suggested_filename
            dl.save_as(target)
            data = open(target, "rb").read()
            r = {"name": dl.suggested_filename, "head": data[:8].hex(), "size": len(data)}
            regions = card.locator('.dg-box[aria-label^="Regions."] rect')
            rx, ry, rw, rh = [float(regions.get_attribute(a)) for a in ("x", "y", "width", "height")]
            link = card.locator(".dg-link").filter(has=page.locator("path")).all()
            target_path = None
            for l in link:
                d = l.locator("path").get_attribute("d")
                n = [float(v) for v in re.findall(r"-?[0-9.]+", d)]
                # the Territories to Regions link ends on the right edge of Regions
                if abs(n[6] - (rx + rw)) < 0.01 and abs(n[7] - (ry + rh / 2)) < 0.01:
                    target_path = n
            bx, by = bezier(target_path, 0.5)
            linkc = HEX(token("--link"))
            r.update(sampler.evaluate(pixel_js, {"b64": base64.b64encode(data).decode(), "bx": int((rx + rw / 2) * 2), "by": int((ry + rh / 2) * 2), "lx": int(bx * 2), "ly": int(by * 2), "linkColor": linkc}))
            r["fill"] = HEX(token("--box")); r["surface"] = HEX(token("--surface")); r["link_color"] = linkc
            return r
        res["png_diagram"] = save_png("diagram")
        card.locator(".viewseg button", has_text="List").click()
        res["png_list_state"] = save_png("list")
        card.locator(".viewseg button", has_text="Diagram").click()
        res["layout_width"] = float(res["map_viewbox"].split()[2])
        sampler.close()
        # forced failure
        page.evaluate("(() => { window.__realToBlob = HTMLCanvasElement.prototype.toBlob; HTMLCanvasElement.prototype.toBlob = function (cb) { cb(null); }; })()")
        card.locator("button", has_text="Save as image").click()
        res["png_failure_note"] = card.locator(".note").first.inner_text()
        page.evaluate("(() => { HTMLCanvasElement.prototype.toBlob = window.__realToBlob; })()")

        # ---- collapsing the map card and the About entry
        map_chev = card.locator(".chev")
        res["map_chev"] = [map_chev.count(), map_chev.get_attribute("aria-expanded"), map_chev.get_attribute("aria-label")]
        map_chev.click()
        res["map_collapsed"] = [card.evaluate("e => e.classList.contains('collapsed')"), card.locator(".dg-svg").is_hidden(), card.locator("button", has_text="Save as image").is_hidden()]
        res["map_collapsed_line"] = card.locator(".qline").inner_text()
        map_chev.click()
        res["map_expanded_again"] = card.locator(".dg-svg").is_visible()
        page.click("#examples summary") if not page.evaluate("document.getElementById('examples').open") else None
        page.locator("#groups .chip", has_text="About this database").click()
        page.wait_for_selector(".reply:last-of-type p.paragraph")
        about = last()
        res["about_chev"] = [about.locator(".chev").count(), about.locator(".chev").get_attribute("aria-expanded")]
        about.locator(".chev").click()
        res["about_collapsed"] = [about.locator("p.paragraph").is_hidden(), about.locator(".qline").inner_text()]
        about.locator(".chev").click()
        res["about_expanded"] = about.locator("p.paragraph").is_visible()
        about.locator("p.paragraph").dblclick()
        res["about_dblclick_collapsed"] = about.evaluate("e => e.classList.contains('collapsed')")

        # ---- collapsible answer cards
        n_before = page.locator(".reply").count()
        ask("table answer one")
        first = last()
        chev = first.locator(".chev")
        res["chev"] = [chev.count(), chev.get_attribute("aria-expanded"), chev.get_attribute("aria-label"), chev.get_attribute("type"), chev.evaluate("e => e.tagName")]
        res["chev_first_in_header"] = first.locator(".reply-top").evaluate("e => e.firstElementChild.classList.contains('chev')")
        res["chev_text"] = chev.inner_text()
        chev.click()
        res["after_click"] = [first.evaluate("e => e.classList.contains('collapsed')"), chev.get_attribute("aria-expanded"), chev.get_attribute("aria-label")]
        res["focus_on_chev_after_click"] = page.evaluate("document.activeElement.classList.contains('chev')")
        box = first.bounding_box()
        res["collapsed_height"] = box["height"]
        res["collapsed_visible"] = {
            "pill": first.locator(".pill").first.is_visible(), "meta": first.locator(".reply-top .meta").first.is_visible(),
            "qline": first.locator(".qline").is_visible(), "qline_text": first.locator(".qline").inner_text(),
            "table": first.locator("table").is_hidden(), "seg": first.locator(".viewseg").is_hidden(), "csv": first.locator("button", has_text="CSV").is_hidden(),
        }
        res["collapsed_doc_overflow"] = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        chev.click()
        res["after_second_click"] = [first.evaluate("e => e.classList.contains('collapsed')"), chev.get_attribute("aria-expanded")]
        chev.focus(); page.keyboard.press("Enter")
        res["enter"] = [first.evaluate("e => e.classList.contains('collapsed')"), chev.get_attribute("aria-expanded"), page.evaluate("document.activeElement.classList.contains('chev')")]
        page.keyboard.press("Space")
        res["space"] = [first.evaluate("e => e.classList.contains('collapsed')"), chev.get_attribute("aria-expanded"), page.evaluate("document.activeElement.classList.contains('chev')")]
        # double-click on the header meta text
        first.locator(".reply-top .meta").first.dblclick()
        res["dbl_meta"] = [first.evaluate("e => e.classList.contains('collapsed')"), page.evaluate("window.getSelection().toString()"), chev.get_attribute("aria-expanded")]
        first.locator(".reply-top .meta").first.dblclick()
        res["dbl_meta_back"] = first.evaluate("e => e.classList.contains('collapsed')")
        # double-click that must do nothing
        first.locator(".viewseg button", has_text="Bar").dblclick() if not first.locator(".viewseg button", has_text="Bar").is_disabled() else first.locator(".viewseg button", has_text="Table").dblclick()
        res["dbl_view_button"] = first.evaluate("e => e.classList.contains('collapsed')")
        first.locator("button", has_text="CSV").dblclick()
        res["dbl_csv"] = first.evaluate("e => e.classList.contains('collapsed')")
        first.locator('.viewseg button', has_text="Table").click()
        first.locator("td").first.dblclick()
        res["dbl_cell"] = [first.evaluate("e => e.classList.contains('collapsed')"), page.evaluate("window.getSelection().toString()")]
        first.locator("table").dblclick()
        res["dbl_body"] = first.evaluate("e => e.classList.contains('collapsed')")

        # a chart that was open when the card was collapsed
        ask("bar chart answer please")
        chart = last()
        res["chart_view"] = chart.locator('.viewseg button[aria-pressed="true"]').inner_text()
        before = chart.locator("svg.svg").bounding_box()
        chart.locator(".chev").click()
        res["chart_hidden_when_collapsed"] = chart.locator("svg.svg").is_hidden()
        chart.locator(".chev").click()
        after = chart.locator("svg.svg").bounding_box()
        res["chart_size"] = [before["width"], before["height"], after["width"], after["height"]]
        res["chart_marks"] = chart.locator(".mark").count()
        res["chart_view_after"] = chart.locator('.viewseg button[aria-pressed="true"]').inner_text()
        # hovering a bar still works after the expand
        chart.locator(".mark").first.hover()
        res["chart_tip_after"] = chart.locator(".tip").first.is_visible()

        # selecting a collapsed card updates the report
        ask("select second question")
        second = last()
        first.locator(".chev").click()
        second.locator(".reply-top .meta").first.click()
        first.locator(".qline").click()
        res["report_after_select_collapsed"] = page.locator("#report").inner_text()
        res["first_aria_current"] = first.get_attribute("aria-current")

        # memory is untouched by collapsing
        res["count_before_collapse_check"] = page.locator("#count").inner_text()
        second.locator(".chev").click()
        res["count_after_collapse"] = page.locator("#count").inner_text()
        requests.clear()
        ask("third history question")
        res["history_questions"] = [t["question"] for t in requests[-1]["history"]]
        res["history_keys"] = sorted(requests[-1]["history"][0].keys())
        res["new_answer_collapsed"] = last().evaluate("e => e.classList.contains('collapsed')")
        res["older_still_collapsed"] = [first.evaluate("e => e.classList.contains('collapsed')"), second.evaluate("e => e.classList.contains('collapsed')")]
        # new session clears everything
        page.click("#newsess")
        res["after_new_session"] = page.locator(".reply").count()
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
    INTERPRETER is None, reason="no interpreter with playwright importable; the diagram browser test is skipped"
)

SCENES = ["light1280", "dark1280", "light420"]


@pytest.fixture(scope="module")
def results(tmp_path_factory, real_db_path):
    folder = tmp_path_factory.mktemp("diagram_browser")
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
        timeout=400,
    )
    if done.returncode == 3 and "BROWSER_UNAVAILABLE" in done.stdout:
        pytest.skip("headless browser is not installed: " + done.stdout.strip())
    assert done.returncode == 0, done.stderr[-2500:]
    return json.loads(done.stdout)


def test_no_console_or_page_errors_and_the_csp_is_respected(results):
    assert results["errors"] == []


@pytest.mark.parametrize("scene", SCENES)
def test_the_map_card_opens_on_the_diagram_with_13_boxes_and_13_links(results, scene):
    r = results[scene]
    assert r["map_boxes"] == 13 and r["map_links"] == 13 and r["map_self_links"] == 1
    assert r["map_legend"] == "A dot marks the table that holds the foreign key; the line runs to the table it points at. Lines pass behind boxes."
    assert r["map_meta"] == "13 tables, 13 foreign keys"
    assert r["map_pill"] == "Built in, no model call"
    assert r["map_control"] == ["Diagram", "List"] and r["map_pressed"] == "Diagram"
    assert r["map_svg_aria"] == "Diagram of 13 tables and 13 foreign keys" and r["map_svg_role"] == "group"
    assert r["map_links_hidden"] == "true"
    assert r["map_box_tabindex"] == "0"
    assert r["map_orders_aria"] == "Orders. FK: CustomerID, EmployeeID, ShipVia"
    assert r["map_orders_title"] == "FK: CustomerID, EmployeeID, ShipVia"
    assert r["map_viewbox"] == "0 0 808 466"


@pytest.mark.parametrize("scene", SCENES)
def test_the_diagram_keeps_a_minimum_width_and_the_page_never_scrolls_sideways(results, scene):
    r = results[scene]
    assert r["map_min_width"] == 640
    assert r["map_doc_overflow"] <= 0 and r["overflow"] <= 0 and r["collapsed_doc_overflow"] <= 0
    # The diagram never shrinks below 640 units, so a narrow card scrolls inside its own box.
    assert r["map_scroll_inside"]["scroll"] >= 640
    if scene == "light420":
        assert r["map_scroll_inside"]["scroll"] > r["map_scroll_inside"]["client"]


@pytest.mark.parametrize("scene", SCENES)
def test_the_list_shows_todays_list_and_the_diagram_comes_back(results, scene):
    r = results[scene]
    assert r["list_items"] == 13 and r["list_visible"] is True and r["diagram_hidden_in_list"] is True
    assert r["list_first"].startswith("Categories") and "Columns:" in r["list_first"]
    assert r["diagram_back"] is True


@pytest.mark.parametrize("scene", SCENES)
def test_hover_and_focus_light_a_table_its_links_and_neighbours(results, scene):
    r = results[scene]
    assert r["hover_hi_boxes"] == ["Customers", "Employees", "Order Details", "Orders", "Shippers"]
    assert r["hover_dim_boxes"] == 8
    assert r["hover_hi_links"] == 4 and r["hover_dim_links"] == 9
    assert r["after_leave"] == [0, 0]
    assert r["focus_hi_boxes"] == 5
    assert r["after_escape"] == [0, 0]
    assert r["after_blur_then_focus_other"] == 2
    assert r["after_blur"] == [0, 0]


@pytest.mark.parametrize("scene", SCENES)
def test_save_as_image_gives_a_real_diagram_png_from_both_states(results, scene):
    r = results[scene]
    for key in ("png_diagram", "png_list_state"):
        png = r[key]
        assert png["name"] == "northwind-database-map.png"
        assert png["head"] == "89504e470d0a1a0a"
        assert png["size"] > 5000
        assert png["width"] >= 2 * r["layout_width"]
        assert png["box"] == png["fill"], (key, png["box"], png["fill"])
        assert png["corner"] == png["surface"]
        assert png["link"] <= 40, (key, png["link"])
        assert png["fill"] != png["surface"]


def test_light_and_dark_pngs_differ(results):
    light, dark = results["light1280"]["png_diagram"], results["dark1280"]["png_diagram"]
    assert light["corner"] != dark["corner"] and light["box"] != dark["box"]
    assert max(dark["corner"]) < 80 and min(light["corner"]) > 200


@pytest.mark.parametrize("scene", SCENES)
def test_a_failed_export_shows_the_plain_note(results, scene):
    assert results[scene]["png_failure_note"] == "Could not save the image in this browser."


@pytest.mark.parametrize("scene", SCENES)
def test_the_built_in_entries_have_the_chevron_and_collapse(results, scene):
    r = results[scene]
    assert r["map_chev"] == [1, "true", "Collapse this answer"]
    assert r["map_collapsed"] == [True, True, True] and r["map_collapsed_line"] == "Database map"
    assert r["map_expanded_again"] is True
    assert r["about_chev"] == [1, "true"]
    assert r["about_collapsed"] == [True, "About this database"] and r["about_expanded"] is True
    assert r["about_dblclick_collapsed"] is False


@pytest.mark.parametrize("scene", SCENES)
def test_the_chevron_is_a_real_button_that_collapses_and_expands(results, scene):
    r = results[scene]
    assert r["chev"] == [1, "true", "Collapse this answer", "button", "BUTTON"]
    assert r["chev_first_in_header"] is True and r["chev_text"] == ""
    assert r["after_click"] == [True, "false", "Expand this answer"]
    assert r["focus_on_chev_after_click"] is True
    assert r["after_second_click"] == [False, "true"]
    assert r["enter"] == [True, "false", True]
    assert r["space"] == [False, "true", True]


@pytest.mark.parametrize("scene", SCENES)
def test_a_collapsed_card_is_one_line_with_the_pill_the_meta_and_the_question(results, scene):
    r = results[scene]
    if scene != "light420":
        assert r["collapsed_height"] < 64
    else:
        assert r["collapsed_height"] < 90
    visible = r["collapsed_visible"]
    assert visible["pill"] and visible["meta"] and visible["qline"]
    assert visible["qline_text"] == "table answer one"
    assert visible["table"] and visible["seg"] and visible["csv"]


@pytest.mark.parametrize("scene", SCENES)
def test_a_double_click_on_the_header_collapses_and_leaves_no_word_selected(results, scene):
    r = results[scene]
    assert r["dbl_meta"] == [True, "", "false"]
    assert r["dbl_meta_back"] is False


@pytest.mark.parametrize("scene", SCENES)
def test_a_double_click_on_buttons_cells_and_body_text_does_not_collapse(results, scene):
    r = results[scene]
    assert r["dbl_view_button"] is False and r["dbl_csv"] is False
    assert r["dbl_cell"][0] is False and r["dbl_body"] is False


@pytest.mark.parametrize("scene", SCENES)
def test_a_chart_has_a_size_again_after_the_card_is_expanded(results, scene):
    r = results[scene]
    assert r["chart_view"] == "Bar" and r["chart_hidden_when_collapsed"] is True
    before_w, before_h, after_w, after_h = r["chart_size"]
    assert after_w > 0 and after_h > 0 and after_w == pytest.approx(before_w, abs=1) and after_h == pytest.approx(before_h, abs=1)
    assert r["chart_marks"] == 5 and r["chart_view_after"] == "Bar" and r["chart_tip_after"] is True


@pytest.mark.parametrize("scene", SCENES)
def test_selecting_a_collapsed_card_still_updates_the_report(results, scene):
    r = results[scene]
    assert "SELECT 'table answer o" in r["report_after_select_collapsed"]
    assert r["first_aria_current"] == "true"


@pytest.mark.parametrize("scene", SCENES)
def test_collapsing_does_not_change_the_memory_or_the_history_sent(results, scene):
    r = results[scene]
    assert r["count_after_collapse"] == r["count_before_collapse_check"]
    assert "table answer one" in r["history_questions"] and "select second question" in r["history_questions"]
    assert r["history_keys"] == ["columns", "outcome", "question", "row_count", "sql"]


@pytest.mark.parametrize("scene", SCENES)
def test_a_new_answer_is_expanded_and_new_session_clears_the_thread(results, scene):
    r = results[scene]
    assert r["new_answer_collapsed"] is False
    assert r["older_still_collapsed"] == [True, True]
    assert r["after_new_session"] == 0
