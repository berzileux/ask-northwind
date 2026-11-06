import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app
from conftest import file_hash

INDEX = Path(__file__).parent / "index.html"
SOURCE = INDEX.read_text()

EXAMPLE_QUESTIONS = [
    "Top 5 customers by revenue",
    "Which employees sold the most in 2016?",
    "Which employee sold the most, and which sold the least?",
    "How many products are discontinued?",
]


@pytest.fixture(scope="module", autouse=True)
def source_unchanged(db_path):
    before = file_hash(db_path)
    yield
    assert file_hash(db_path) == before


@pytest.fixture
def client(monkeypatch, db_path):
    monkeypatch.setattr(app, "DB_PATH", db_path)
    with TestClient(app.app) as test_client:
        yield test_client


def nonce_of(response):
    policy = response.headers["Content-Security-Policy"]
    return re.search(r"script-src 'nonce-([^']+)'", policy).group(1)


def test_page_is_html_with_a_matching_nonce(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")

    policy = response.headers["Content-Security-Policy"]
    nonce = nonce_of(response)
    assert f"style-src 'nonce-{nonce}'" in policy
    assert "__NONCE__" not in response.text
    assert re.search(rf'<script nonce="{re.escape(nonce)}"', response.text)
    assert re.search(rf'<style nonce="{re.escape(nonce)}"', response.text)


def test_policy_is_exactly_the_agreed_one(client):
    response = client.get("/")
    nonce = nonce_of(response)
    assert response.headers["Content-Security-Policy"] == (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        f"style-src 'nonce-{nonce}'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    )


def test_each_response_gets_a_new_nonce(client):
    assert nonce_of(client.get("/")) != nonce_of(client.get("/"))


def test_security_headers_on_page_and_api(client):
    api = client.post("/api/ask", json={"question": ""})
    for response in (client.get("/"), api):
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["X-Frame-Options"] == "DENY"
    assert api.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "forbidden",
    ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "https://"],
)
def test_page_has_no_dangerous_calls_or_external_urls(forbidden):
    assert forbidden not in SOURCE


SVG_NAMESPACE = "http://www.w3.org/2000/svg"


def test_the_only_http_text_is_the_single_svg_namespace_identifier():
    assert SOURCE.count("http://") == 1
    assert SOURCE.count(SVG_NAMESPACE) == 1
    assert f'var SVG_NS = "{SVG_NAMESPACE}";' in SOURCE


def test_page_has_no_inline_handlers_style_attributes_or_external_resources():
    assert not re.search(r"\son[a-z]+\s*=", SOURCE)
    assert not re.search(r"\sstyle\s*=", SOURCE)
    assert not re.search(r"<script[^>]*\ssrc\s*=", SOURCE)
    assert not re.search(r"<link[^>]*href", SOURCE)


def test_page_is_ascii_only():
    SOURCE.encode("ascii")


SCRIPT_IDS = [
    "q", "askform", "thread", "report", "go", "groups", "count", "empty",
    "examples", "rstate", "limits", "newsess", "memoryhint",
    "modelpill",
]


def test_every_id_the_script_looks_up_exists_exactly_once():
    looked_up = set(re.findall(r'getElementById\("([^"]+)"\)', SOURCE))
    assert looked_up == set(SCRIPT_IDS)
    for element_id in SCRIPT_IDS:
        assert SOURCE.count(f'id="{element_id}"') == 1, element_id


@pytest.mark.parametrize("question", EXAMPLE_QUESTIONS)
def test_example_questions_are_in_the_page(question):
    assert question in SOURCE


def test_page_never_mentions_1997():
    assert "1997" not in SOURCE


def test_example_questions_are_in_the_agreed_order():
    positions = [SOURCE.index(q) for q in EXAMPLE_QUESTIONS]
    assert positions == sorted(positions)


def test_page_has_theme_buttons_titles_and_notice():
    for label in ["Dark", "Light", "System"]:
        assert re.search(rf'<button[^>]*data-t="{label.lower()}"[^>]*>{label}</button>', SOURCE)
    assert "Thread" in SOURCE
    assert "Sandbox report" in SOURCE
    assert (
        "Demo only. Do not enter private information. Questions go to a hosted AI "
        "model and may be used by its provider to improve its models."
    ) in SOURCE


def test_page_has_one_style_and_one_script_with_the_nonce_placeholder():
    assert len(re.findall(r"<style\b", SOURCE)) == 1
    assert len(re.findall(r"<script\b", SOURCE)) == 1
    assert '<style nonce="__NONCE__">' in SOURCE
    assert '<script nonce="__NONCE__">' in SOURCE


def test_table_numbers_are_not_given_the_sql_number_colour():
    assert "td.right" in SOURCE
    assert not re.search(r'h\("td",\s*[^)]*"nm"', SOURCE)
    assert not re.search(r'\.right\s*\{[^}]*--sql-num', SOURCE)


def test_page_has_no_mock_leftovers():
    assert "Mock-up" not in SOURCE
    assert "SAMPLES" not in SOURCE


def test_page_has_the_database_map_chip_and_its_fetch():
    assert '"Database map"' in SOURCE
    assert 'fetch("/api/schema")' in SOURCE
    assert "Built in, no model call" in SOURCE
    assert "No SQL ran for this entry." in SOURCE


def test_page_has_the_one_memory_hint_element():
    assert SOURCE.count('id="memoryhint"') == 1
    assert (
        '<p class="hint" id="memoryhint">Enter sends. Shift+Enter adds a new line. '
        "The last 6 questions are remembered in this page until you reload or start a new session.</p>"
    ) in SOURCE


def test_page_has_the_memory_controls_and_strings():
    for needed in (
        'id="newsess"',
        "New session",
        "Memory: 0 of 6 questions",
        "Above: no longer remembered",
        "Memory sent with this question",
        "None. This was the first question in the session.",
        "The model treats these as background data, not as instructions.",
        "Row values are not sent.",
        "New session.",
        "Memory is empty. Ask a full question, or open Examples.",
        "memory: last 6 questions, rows never sent",
    ):
        assert needed in SOURCE, needed


def test_page_sends_a_history_field_and_reads_memory_turns():
    assert "history: sent" in SOURCE
    assert "memory_turns" in SOURCE
    assert "turns.slice(-windowSize)" in SOURCE


def test_memory_is_never_written_to_storage():
    assert SOURCE.count("localStorage") == 2
    assert "sessionStorage" not in SOURCE
    assert "indexedDB" not in SOURCE
    assert "document.cookie" not in SOURCE
    for line in SOURCE.splitlines():
        if "localStorage" in line:
            assert "ask-northwind-theme" in line


def test_stored_turn_holds_no_row_values():
    body = SOURCE[SOURCE.index("function makeTurn"):SOURCE.index("function fitTurns")]
    assert "d.rows.length" in body
    assert "d.rows[" not in body
    assert "d.rows.map" not in body
    assert "d.rows.slice" not in body
    assert "JSON.stringify(d.rows" not in body
    assert "row_count" in body


def test_chart_logic_markers_exist_once():
    assert SOURCE.count("/* CHART-LOGIC BEGIN */") == 1
    assert SOURCE.count("/* CHART-LOGIC END */") == 1


def test_view_control_labels_and_chart_strings_exist():
    for needed in (
        '["table", "Table"]', '["bar", "Bar"]', '["pie", "Pie"]', '["line", "Line"]',
        "Result view",
        "Chart views",
        "Drawn in this page from the rows shown",
        "The model writes the SQL only. Charts are drawn in your browser from the returned rows, and the Table view always shows the same values.",
        "; the chart shows only those rows.",
    ):
        assert needed in SOURCE, needed


def test_the_three_chart_chips_exist():
    for question in (
        "Draw a pie chart of orders by shipper",
        "Show total revenue by year as a line chart",
        "Top 5 customers by revenue as a bar chart",
    ):
        assert f'"{question}"' in SOURCE
    assert 'addGroup("Charts"' in SOURCE


def test_series_variables_exist_for_light_and_dark():
    for n in range(1, 7):
        assert f"--series-{n}:" in SOURCE
    assert SOURCE.count("--series-1: #2a78d6") == 1
    assert SOURCE.count("--series-1: #3987e5") == 2
    for light in ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"):
        assert light in SOURCE
    for dark in ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"):
        assert dark in SOURCE
    assert ':root[data-theme="dark"] { --series-1: #3987e5' in SOURCE


def test_no_set_attribute_style_anywhere():
    assert 'setAttribute("style"' not in SOURCE
    assert "setAttribute('style'" not in SOURCE
    assert not re.search(r"\sstyle\s*=", SOURCE)


def test_chart_code_uses_svg_elements_and_text_content_only():
    assert "createElementNS" in SOURCE
    assert SOURCE.count("createElementNS") == 1
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "DOMParser", "<svg"):
        assert forbidden not in SOURCE


def test_tooltip_is_positioned_from_script_with_the_style_property():
    assert "tip.style.left" in SOURCE and "tip.style.top" in SOURCE


def test_the_reply_card_keydown_ignores_other_targets():
    assert 'e.target === reply && (e.key === "Enter" || e.key === " ")' in SOURCE


def test_the_about_chip_and_its_fetch_exist():
    assert '"About this database"' in SOURCE
    assert 'fetch("/api/about")' in SOURCE
    assert "showAbout" in SOURCE
    assert 'addGroup("Built in", [{ label: "Database map", pick: function () { showMap(); } }, { label: "About this database", pick: showAbout }]' in SOURCE


def test_the_paragraph_rule_has_its_threshold():
    assert "var PARAGRAPH_MIN = 120;" in SOURCE
    assert "d.rows[0][0].length > PARAGRAPH_MIN" in SOURCE
    assert 'h("p", "paragraph"' in SOURCE
    assert "d.rows.length === 1 && d.columns.length === 1" in SOURCE
    assert "typeof d.rows[0][0] === \"string\"" in SOURCE


def test_the_about_entry_is_built_in_and_not_remembered():
    block = SOURCE[SOURCE.index("function showAbout"):SOURCE.index("function buildGroups")]
    assert "Built in, no model call" in block
    assert "turns.push" not in block and "makeTurn" not in block
    assert "{ builtin: true }, null" in block


def test_the_three_provider_outcomes_have_their_pills_and_messages():
    for needed in (
        '"Quota used"', '"Slow down"', '"Busy"',
        "The free model quota is used up for now. Please try again in a minute.",
        "The model service is busy or slow right now. Please try again.",
        "You are asking too fast. Please wait ",
        'kind === "quota" || kind === "rate_limited"',
        'kind === "unavailable"',
    ):
        assert needed in SOURCE, needed


def test_only_unavailable_and_busy_get_a_try_again_button():
    body = SOURCE[SOURCE.index("function replyBody"):SOURCE.index("function failureText")]
    quota = body[body.index('kind === "quota" || kind === "rate_limited"'):body.index('kind === "unavailable"')]
    assert "tryAgain" not in quota
    unavailable = body[body.index('kind === "unavailable"'):body.index('kind === "busy"')]
    assert "tryAgain(request)" in unavailable


def test_the_page_reads_retry_after_for_its_own_429():
    assert "res.status === 429" in SOURCE
    assert 'res.headers.get("Retry-After")' in SOURCE
    assert 'body.error_kind !== "rate_limited"' in SOURCE


def test_the_limits_line_reads_the_rate_from_the_server():
    assert "lim.rate_limit_per_minute" in SOURCE
    assert '" questions per minute"' in SOURCE


def test_the_pie_rule_is_three_to_six_parts():
    assert "Pie needs 3 to 6 parts, all above zero; this has " in SOURCE
    assert "Pie needs 2 to 6" not in SOURCE
    assert "n >= 3 && n <= 6" in SOURCE


def luminance(hex_color):
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_the_groups_button_view_and_note_text_exist():
    assert '["groups", "Groups"]' in SOURCE
    assert "One block per row. The page draws no number the SQL did not return." in SOURCE
    assert "Groups is for lists without a number. Use Bar, Pie or Line." in SOURCE
    assert "Groups needs 2 to 60 rows; this has " in SOURCE
    assert "Groups needs 2 to 20 groups; this has " in SOURCE
    assert "Groups needs a group column and an item column." in SOURCE
    assert "Groups needs at most three label columns." in SOURCE
    assert '"Groups of " + items + " by " + info.groupName' in SOURCE
    assert '" grouped by " + info.groupName' in SOURCE


def test_the_groups_css_classes_exist_and_use_the_series_color_token():
    for name in (".gv ", ".gv-title", ".gv-list", ".gv-row", ".gv-label", ".gv-blocks", ".gv-block", ".gv-count", ".gv-note"):
        assert name in SOURCE, name
    block = SOURCE[SOURCE.index(".gv-block {"):SOURCE.index(".gv-count {")]
    assert "background: var(--series-1)" in block and "color: var(--block-ink)" in block
    assert "text-overflow: ellipsis" in block


def test_block_text_contrast_is_at_least_four_and_a_half_to_one_in_light_and_dark():
    import re

    light_fill = re.search(r"^:root \{ --series-1: (#[0-9a-f]{6})", SOURCE, re.M).group(1)
    dark_fill = re.search(r':root\[data-theme="dark"\] \{ --series-1: (#[0-9a-f]{6})', SOURCE).group(1)
    light_ink = re.search(r"^:root \{ --block-ink: (#[0-9a-f]{6})", SOURCE, re.M).group(1)
    dark_ink = re.search(r':root\[data-theme="dark"\] \{ --block-ink: (#[0-9a-f]{6})', SOURCE).group(1)
    assert contrast(light_fill, light_ink) >= 4.5
    assert contrast(dark_fill, dark_ink) >= 4.5
    # White on the light fill would fall short, which is why the ink token is not white.
    assert contrast(light_fill, "#ffffff") < 4.5
    # The system dark scope carries the same ink.
    system_dark = SOURCE[SOURCE.index(':root:not([data-theme="light"]) { --block-ink'):][:80]
    assert dark_ink in system_dark


def test_groups_styling_uses_classes_only():
    block = SOURCE[SOURCE.index("function groupsView"):SOURCE.index("function chartSection")]
    assert "style" not in block
    assert "innerHTML" not in block
    assert ".title = text" in block


def test_narrow_screens_put_the_group_label_above_its_blocks():
    assert "@media (max-width: 520px)" in SOURCE
    narrow = SOURCE[SOURCE.index(".gv-row { grid-template-columns: minmax(0, 1fr) auto; }"):][:260]
    assert ".gv-blocks { grid-column: 1 / -1; }" in narrow


def test_the_chart_views_section_has_a_groups_row():
    assert '["groups", "Groups"]' in SOURCE
    assert '[["bar", "Bar chart"], ["pie", "Pie chart"], ["line", "Line chart"], ["groups", "Groups"]]' in SOURCE


def test_the_refusal_hint_constant_matches_the_repo_refusal_text():
    import re
    from pathlib import Path

    rules = (Path(__file__).parent / "base_rules.md").read_text()
    real = re.search(r"output: SELECT '([^']+)' AS message", rules).group(1)
    assert f'var REFUSAL_TEXT = "{real}";' in SOURCE
    assert real == "I cannot answer that from this database"


def test_the_refusal_hint_and_export_texts_exist():
    for needed in (
        "I can only answer from this database's tables. Database map shows how they connect; About this database says what is in it.",
        '"Save as image"',
        'h("button", "copy hide-c", "CSV")',
        "Could not save the image in this browser.",
        "Could not save the file in this browser.",
        '"northwind-database-map.png"',
        '"CSV holds the " + d.limits.max_rows + " rows shown."',
        "ask-northwind-result-",
    ):
        assert needed in SOURCE, needed


def test_the_refusal_check_compares_the_row_exactly():
    body = SOURCE[SOURCE.index("function isRefusal"):SOURCE.index("function mapCanvas")]
    assert 'd.rows.length === 1 && d.columns.length === 1 && d.columns[0] === "message" && d.rows[0][0] === REFUSAL_TEXT' in body


def test_the_csv_button_is_skipped_for_the_refusal_row_and_the_paragraph_card():
    body = SOURCE[SOURCE.index("function replyBody"):SOURCE.index("function failureText")]
    assert "!refusal && !isParagraph(d) && d.columns.length > 0 && d.rows.length > 0" in body


def test_exports_are_not_remembered_and_never_requery():
    block = SOURCE[SOURCE.index("function saveBlob"):SOURCE.index("function saveMapImage")]
    assert "turns.push" not in block and "fetch(" not in block and "makeTurn" not in block
    export = SOURCE[SOURCE.index("function exportCsv"):SOURCE.index("var REFUSAL_TEXT")]
    assert "csvText(d.columns, d.rows)" in export and "fetch(" not in export


def test_downloads_use_a_temporary_link_and_release_the_object_url():
    save = SOURCE[SOURCE.index("function saveBlob"):SOURCE.index("function exportCsv")]
    assert "link.download = name" in save
    assert "URL.createObjectURL(blob)" in save and "URL.revokeObjectURL(url)" in save
    assert "removeChild(link)" in save


def test_the_map_image_is_drawn_from_theme_tokens_not_a_fixed_palette():
    # The diagram PNG reads the surface, box, link and text tokens of the active theme.
    canvas = SOURCE[SOURCE.index("function mapCanvas"):SOURCE.index("function saveMapImage")]
    for token in ('"--surface"', '"--box"', '"--box-edge"', '"--link"', '"--ink"', '"--muted"', '"--font-ui"', '"--font-mono"'):
        assert token in canvas
    assert "getComputedStyle(document.documentElement)" in canvas
    assert "style" not in canvas.replace("getComputedStyle", "").replace("fillStyle", "").replace("strokeStyle", "")


def test_map_image_label_contrast_is_at_least_four_and_a_half_to_one_in_both_themes():
    import re

    def token(scope_pattern, name):
        return re.search(scope_pattern + r"[^}]*?" + name + r": (#[0-9a-f]{6})", SOURCE, re.S).group(1)

    light = {n: re.search(r"^:root \{\n[^}]*?--" + n + r": (#[0-9a-f]{6})", SOURCE, re.M | re.S).group(1) for n in ("surface", "ink", "muted")}
    dark_scope = ':root\\[data-theme="dark"\\] \\{'
    dark = {n: token(dark_scope, "--" + n) for n in ("surface", "ink", "muted")}
    for theme in (light, dark):
        assert contrast(theme["surface"], theme["ink"]) >= 4.5
        assert contrast(theme["surface"], theme["muted"]) >= 4.5


def test_the_export_blocks_and_classes_exist():
    assert SOURCE.count("/* CSV-LOGIC BEGIN */") == 1 and SOURCE.count("/* CSV-LOGIC END */") == 1
    assert 'h("button", "copy hide-c", "Save as image")' in SOURCE
    assert ".mapbtn" not in SOURCE
    assert 'setAttribute("style"' not in SOURCE


def test_the_diagram_block_markers_and_texts_exist():
    assert SOURCE.count("/* DIAGRAM-LOGIC BEGIN */") == 1 and SOURCE.count("/* DIAGRAM-LOGIC END */") == 1
    assert 'var LEGEND = "A dot marks the table that holds the foreign key; the line runs to the table it points at. Lines pass behind boxes.";' in SOURCE
    assert 'h("button", "", "Diagram")' in SOURCE and 'h("button", "", "List")' in SOURCE
    assert '"Diagram of " + layout.boxes.length + " tables and " + layout.links.length + " foreign keys"' in SOURCE
    assert "Collapse this answer" in SOURCE and "Expand this answer" in SOURCE


def test_the_diagram_tokens_exist_in_light_dark_and_system_dark():
    import re

    assert re.search(r"^:root \{ --box: #eef3fb; --box-edge: #2a78d6; --link: #8e8e93; --hi: #2a78d6; \}", SOURCE, re.M)
    dark = "--box: #16233a; --box-edge: #3987e5; --link: #7c7c84; --hi: #6aa2ff;"
    assert f':root:not([data-theme="light"]) {{ {dark} }}' in SOURCE
    assert f':root[data-theme="dark"] {{ {dark} }}' in SOURCE


def test_diagram_contrast_ratios_in_light_and_dark():
    import re

    def pick(scope, name):
        return re.search(scope + r"[^}]*?--" + name + r": (#[0-9a-f]{6})", SOURCE, re.S).group(1)

    light_scope = r"^:root \{"
    dark_scope = r':root\[data-theme="dark"\] \{'
    for scope in (light_scope, dark_scope):
        flags = re.M if scope == light_scope else 0
        values = {n: re.search(scope + r"[^}]*?--" + n + r": (#[0-9a-f]{6})", SOURCE, flags | re.S).group(1) for n in ("box", "box-edge", "link")}
        base = re.search(scope + r"\n[^}]*?--surface: (#[0-9a-f]{6})", SOURCE, flags | re.S).group(1)
        ink = re.search(scope + r"\n[^}]*?--ink: (#[0-9a-f]{6})", SOURCE, flags | re.S).group(1)
        muted = re.search(scope + r"\n[^}]*?--muted: (#[0-9a-f]{6})", SOURCE, flags | re.S).group(1)
        assert contrast(values["box"], ink) >= 4.5
        assert contrast(values["box"], muted) >= 4.5
        assert contrast(base, values["box-edge"]) >= 3
        assert contrast(base, values["link"]) >= 3


def test_the_diagram_uses_classes_for_colors_and_strokes():
    for name in (".dg-svg", ".dg-link path", ".dg-box rect", ".dg-name", ".dg-sub", ".dg-hi", ".dg-dim", ".dg-legend", ".dg {"):
        assert name in SOURCE, name
    assert "background: var(--surface)" in SOURCE[SOURCE.index(".dg {"):SOURCE.index(".dg-svg")]
    view = SOURCE[SOURCE.index("function diagramView"):SOURCE.index("function mapCanvas")]
    assert "style" not in view and "innerHTML" not in view
    assert 'classList' in view and "createElementNS" not in view
    assert "var SVG_NS" in SOURCE and SOURCE.count("http://www.w3.org/2000/svg") == 1


def test_the_old_text_sheet_image_renderer_is_gone():
    assert "Columns: " in SOURCE[SOURCE.index("function mapBody"):SOURCE.index("function showMap")]
    canvas = SOURCE[SOURCE.index("function mapCanvas"):SOURCE.index("function saveMapImage")]
    assert "wrap(" not in canvas and "Columns:" not in canvas and "textW" not in canvas
    assert "diagramLayout(data)" in SOURCE[SOURCE.index("function saveMapImage"):SOURCE.index("var SVG_NS")]


def test_the_chevron_is_a_button_with_aria_expanded_and_css_drawn_glyph():
    add = SOURCE[SOURCE.index("function addCollapse"):SOURCE.index("function hatched")]
    assert 'h("button", "chev")' in add and 'chevron.type = "button";' in add
    assert 'setAttribute("aria-expanded", "true")' in add
    assert 'setAttribute("aria-label", "Collapse this answer")' in add
    toggle = SOURCE[SOURCE.index("function toggleCard"):SOURCE.index("function addCollapse")]
    assert '"aria-expanded", collapsed ? "false" : "true"' in toggle
    assert "Expand this answer" in toggle
    css = SOURCE[SOURCE.index(".chev {"):SOURCE.index(".qline {")]
    assert "border-right: 2px solid currentColor" in css and "transform: rotate(45deg)" in css
    assert 'content: ""' in css
    assert "emoji" not in SOURCE.lower()


def test_collapse_has_no_animation_and_uses_one_class_per_card():
    css = SOURCE[SOURCE.index(".reply.collapsed > :not(.reply-top)"):SOURCE.index("@media (pointer: coarse) { .chev")]
    assert "transition" not in css and "animation" not in css
    assert "reply.collapsed .hide-c" in css and ".reply.collapsed .qline" in css


def test_the_header_double_click_ignores_buttons_links_and_fields_and_clears_the_selection():
    add = SOURCE[SOURCE.index("function addCollapse"):SOURCE.index("function hatched")]
    assert 'top.addEventListener("dblclick"' in add
    assert 'closest("button, a, input, textarea, select")' in add
    assert "removeAllRanges()" in add
    assert "reply.addEventListener(\"dblclick\"" not in SOURCE


def test_the_intent_block_markers_constants_and_note_exist():
    assert SOURCE.count("/* INTENT-LOGIC BEGIN */") == 1 and SOURCE.count("/* INTENT-LOGIC END */") == 1
    assert 'var DIAGRAM_WORDS = ["diagram", "diagrams", "erd", "er diagram", "entity relationship", "entity-relationship", "schematic", "schema diagram"];' in SOURCE
    assert 'var CHART_WORDS = ["pie", "bar", "line", "chart", "charts", "graph", "plot", "histogram"];' in SOURCE
    assert 'var DIAGRAM_NOTE = "Shown from the built-in Database map because the question asked for a diagram.";' in SOURCE


def test_the_diagram_check_runs_before_any_request_in_the_ask_path():
    ask = SOURCE[SOURCE.index("function askText"):SOURCE.index("function addGroup")]
    assert ask.index("wantsDiagram(text)") < ask.index("send({")
    assert "showMap(text, DIAGRAM_NOTE);\n      return;" in ask


def test_the_chip_still_calls_the_map_with_no_arguments():
    assert 'pick: function () { showMap(); }' in SOURCE
    assert 'typeof label === "string" ? label : "Database map"' in SOURCE


def test_the_model_pill_markup_and_classes_exist():
    assert 'id="modelpill"' in SOURCE and SOURCE.count('class="model-pill"') == 1
    assert 'class="mdot" aria-hidden="true"' in SOURCE
    pill = SOURCE[SOURCE.index('id="modelpill"'):]
    assert pill[: pill.index("</div>")].count("hidden") >= 1
    assert SOURCE.index('<h1>') < SOURCE.index('id="modelpill"') < SOURCE.index('class="tools"') < SOURCE.index('class="sub"')
    for rule in (".model-pill {", ".model-pill .mdot {", ".model-pill.stopped .mdot {", ".model-pill .mname {"):
        assert rule in SOURCE
    assert "text-overflow: ellipsis" in SOURCE[SOURCE.index(".model-pill .mname"):][:200]
    assert "var(--font-mono)" in SOURCE[SOURCE.index(".model-pill {"):][:400]


def test_the_dot_states_are_classes_and_the_name_comes_from_the_limits_object():
    logic = SOURCE[SOURCE.index("var modelPill"):SOURCE.index("function showLimits")]
    assert 'classList.toggle("stopped"' in logic
    assert ".style" not in logic and "setAttribute(\"style\"" not in logic
    assert "showModelName(lim.model)" in SOURCE
    assert "textContent = modelName" in logic
    assert "Last request failed: " in logic and '"quota used"' in logic and '"model busy"' in logic
    assert 'request.url === "/api/ask"' in SOURCE
    assert SOURCE.count("noteModelOutcome(") == 2


def test_no_model_name_is_hard_coded_in_the_page():
    assert "gemini" not in SOURCE.lower()


def _luminance(hex_color):
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _ratio(a, b):
    high, low = sorted([_luminance(a), _luminance(b)], reverse=True)
    return (high + 0.05) / (low + 0.05)


def _tokens(block_start, block_end=None):
    import re

    start = SOURCE.index(block_start)
    end = SOURCE.index(block_end, start) if block_end else start + 1500
    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})", SOURCE[start:end]))


@pytest.mark.parametrize(
    "start,end",
    [(":root {", "@media"), ("@media (prefers-color-scheme: dark)", "}\n}"), (':root[data-theme="dark"]', "color-scheme: dark;")],
)
def test_pill_contrast_meets_the_targets_in_light_dark_and_system_dark(start, end):
    tokens = _tokens(start, end)
    if "dot-ok" not in tokens:
        tokens = {**_tokens(":root {", "@media"), **tokens}
    assert _ratio(tokens["ink"], tokens["surface"]) >= 4.5
    assert _ratio(tokens["dot-ok"], tokens["surface"]) >= 3.0
    assert _ratio(tokens["strong"], tokens["surface"]) >= 3.0


def test_the_heading_is_only_the_title_and_the_pill_is_outside_it():
    start = SOURCE.index("<h1>")
    end = SOURCE.index("</h1>")
    assert SOURCE[start:end + 5] == "<h1>ask-northwind</h1>"
    assert SOURCE.index('id="modelpill"') > end
    assert "<title>" in SOURCE and "ask-northwind" in SOURCE[SOURCE.index("<title>"):SOURCE.index("</title>")]


def test_the_subtitle_text_is_unchanged_and_has_no_width_limit():
    text = "Ask the Northwind sample database a question in plain English. The page shows the answer and the SQL that ran."
    assert f'<p class="sub">{text}</p>' in SOURCE
    rule = SOURCE[SOURCE.index(".sub {"):].split("}", 1)[0]
    assert "max-width" not in rule and "ch;" not in rule
    assert "text-overflow" not in rule and "overflow" not in rule
