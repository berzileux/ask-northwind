import inspect
import sys
import threading

import pytest
from fastapi.testclient import TestClient

import app
import pipeline
from conftest import INJECTION_TEXT, file_hash
from model import ModelError
from prompt import QUESTION_END


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


def use_model(monkeypatch, text, calls=None):
    def generate(prompt):
        if calls is not None:
            calls.append(prompt)
        return text

    monkeypatch.setattr(app, "GENERATE", generate)


def ask(client, question):
    return client.post("/api/ask", json={"question": question})


def single_cell(client, monkeypatch, sql):
    use_model(monkeypatch, sql)
    response = ask(client, "one cell")
    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None, body
    return body["rows"][0][0]


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_documentation_pages_are_off(client, path):
    assert client.get(path).status_code == 404


def test_startup_fails_without_the_database(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "missing.db")
    with pytest.raises(RuntimeError):
        with TestClient(app.app):
            pass


def test_valid_question_gives_rows_and_limits(client, monkeypatch):
    use_model(monkeypatch, "SELECT CustomerID, CompanyName FROM Customers ORDER BY 1")
    response = ask(client, "list customers")
    body = response.json()
    assert response.status_code == 200
    assert body["error_kind"] is None
    assert body["columns"] == ["CustomerID", "CompanyName"]
    assert body["rows"]
    assert body["source"] == "model"
    assert body["limits"]["timeout_s"] == pipeline.QUERY_TIMEOUT_S
    assert body["limits"]["max_rows"] == app.MAX_ROWS


def test_row_limit_is_the_sandbox_default():
    import sandbox

    default = inspect.signature(sandbox.run_query).parameters["max_rows"].default
    assert app.MAX_ROWS == default


@pytest.mark.parametrize(
    "question",
    ["", "   ", "x" * 501, QUESTION_END],
    ids=["empty", "blank", "too_long", "end_delimiter"],
)
def test_bad_questions_never_reach_the_model(client, monkeypatch, question):
    calls = []
    use_model(monkeypatch, "SELECT 1", calls)
    response = ask(client, question)
    assert response.status_code == 200
    assert response.json()["error_kind"] == "input"
    assert calls == []


def test_question_of_2001_characters_is_422(client):
    assert ask(client, "x" * 2001).status_code == 422


def test_missing_question_field_is_422(client):
    assert client.post("/api/ask", json={}).status_code == 422


def test_body_that_is_not_json_is_422(client):
    response = client.post(
        "/api/ask", content=b"not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 422


def test_oversized_content_length_is_413(client):
    response = client.post(
        "/api/ask",
        content=b"x" * (app.MAX_BODY_BYTES + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM Customers",
        "SELECT 1; DROP TABLE Customers",
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA table_info(Customers)",
    ],
    ids=["delete", "stacked", "attach", "pragma"],
)
def test_hostile_model_output_is_rejected(client, monkeypatch, sql):
    use_model(monkeypatch, sql)
    response = ask(client, "anything")
    body = response.json()
    assert response.status_code == 200
    assert body["error_kind"] == "rejected"
    assert body["rows"] == []


def test_failing_model_gives_generic_model_error(client, monkeypatch):
    def generate(prompt):
        raise ModelError("secret detail from the provider")

    monkeypatch.setattr(app, "GENERATE", generate)
    response = ask(client, "anything")
    body = response.json()
    assert response.status_code == 200
    assert body["error_kind"] == "model"
    assert "secret detail" not in response.text


def test_full_gate_gives_busy_and_is_free_afterwards(client, monkeypatch):
    use_model(monkeypatch, "SELECT 1")
    monkeypatch.setattr(pipeline, "WAIT_FOR_SLOT_S", 0.2)
    for _ in range(pipeline.GATE_SLOTS):
        pipeline.GATE.acquire()
    try:
        response = ask(client, "anything")
    finally:
        for _ in range(pipeline.GATE_SLOTS):
            pipeline.GATE.release()
    assert response.status_code == 200
    assert response.json()["error_kind"] == "busy"
    assert pipeline.GATE.acquire(blocking=False)
    pipeline.GATE.release()


def test_blob_becomes_a_marker(client, monkeypatch):
    assert single_cell(client, monkeypatch, "SELECT x'00ff'") == "<binary, 2 bytes>"


def test_infinity_becomes_text(client, monkeypatch):
    assert single_cell(client, monkeypatch, "SELECT 1e999") == "Infinity"


def test_negative_infinity_becomes_text(client, monkeypatch):
    assert single_cell(client, monkeypatch, "SELECT -1e999") == "-Infinity"


def test_large_integer_becomes_a_string(client, monkeypatch):
    assert single_cell(client, monkeypatch, "SELECT 9007199254740993") == "9007199254740993"


def test_small_integer_stays_an_int(client, monkeypatch):
    value = single_cell(client, monkeypatch, "SELECT 42")
    assert value == 42 and isinstance(value, int)


def test_null_stays_null(client, monkeypatch):
    assert single_cell(client, monkeypatch, "SELECT NULL") is None


def test_json_safe_function_directly():
    assert app.json_safe(b"abc") == "<binary, 3 bytes>"
    assert app.json_safe(float("nan")) == "NaN"
    assert app.json_safe(2**53 - 1) == 2**53 - 1
    assert app.json_safe(2**53) == str(2**53)
    assert app.json_safe(True) is True


def test_injection_row_comes_back_as_plain_data(client, monkeypatch):
    sql = "SELECT ContactName FROM Customers WHERE ContactName LIKE '%DROP TABLE%'"
    use_model(monkeypatch, sql)
    body = ask(client, "who is odd?").json()
    assert body["rows"] == [[INJECTION_TEXT]]


def test_demo_list_has_id_and_label_only(client):
    demos = client.get("/api/sandbox-demos").json()
    assert [d["id"] for d in demos] == ["delete", "stacked", "slow"]
    assert all(set(d) == {"id", "label"} for d in demos)


@pytest.mark.parametrize("demo_id", ["delete", "stacked"])
def test_destructive_demos_are_rejected_and_leave_the_database_alone(
    client, db_path, demo_id
):
    before = file_hash(db_path)
    body = client.post("/api/sandbox-demo", json={"id": demo_id}).json()
    assert body["error_kind"] == "rejected"
    assert body["source"] == "demo"
    assert file_hash(db_path) == before


def test_slow_demo_times_out_on_the_generated_database(client, monkeypatch):
    # The generated database names the table OrderDetails, so the fixed SQL is
    # swapped for the equivalent query that exists there.
    monkeypatch.setitem(
        app.DEMOS["slow"],
        "sql",
        "SELECT COUNT(*) FROM OrderDetails a, OrderDetails b, "
        "OrderDetails c, OrderDetails d",
    )
    monkeypatch.setattr(app, "DEMO_TIMEOUT_S", 0.5)
    body = client.post("/api/sandbox-demo", json={"id": "slow"}).json()
    assert body["error_kind"] == "timeout"
    assert body["limits"]["timeout_s"] == 0.5


def test_slow_demo_times_out_on_the_real_database(monkeypatch, real_db_path):
    monkeypatch.setattr(app, "DB_PATH", real_db_path)
    monkeypatch.setattr(app, "DEMO_TIMEOUT_S", 0.5)
    with TestClient(app.app) as real_client:
        body = real_client.post("/api/sandbox-demo", json={"id": "slow"}).json()
    assert body["error_kind"] == "timeout"


def test_unknown_demo_is_404(client):
    assert client.post("/api/sandbox-demo", json={"id": "nope"}).status_code == 404


def test_demo_request_cannot_carry_sql(client):
    body = client.post(
        "/api/sandbox-demo", json={"id": "delete", "sql": "SELECT 1"}
    ).json()
    assert body["error_kind"] == "rejected"
    assert body["sql"] == "DELETE FROM Customers;"


def test_ask_handlers_are_plain_functions():
    assert not inspect.iscoroutinefunction(app.api_ask)
    assert not inspect.iscoroutinefunction(app.api_sandbox_demo)


def test_two_threads_both_get_answers(monkeypatch, db_path):
    monkeypatch.setattr(app, "DB_PATH", db_path)
    use_model(monkeypatch, "SELECT 1")
    codes = []

    def worker():
        thread_client = TestClient(app.app)
        codes.append(ask(thread_client, "one").status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert codes == [200, 200]


def test_importing_app_does_not_load_the_sdk():
    assert "app" in sys.modules
    assert "google.genai" not in sys.modules


def test_success_has_a_numeric_query_time(client, monkeypatch):
    use_model(monkeypatch, "SELECT 1")
    body = ask(client, "one").json()
    assert isinstance(body["query_s"], float)
    assert 0 <= body["query_s"] <= body["elapsed_s"]


def test_rejected_has_a_null_query_time(client, monkeypatch):
    use_model(monkeypatch, "DELETE FROM Customers")
    body = ask(client, "anything").json()
    assert body["error_kind"] == "rejected"
    assert body["query_s"] is None


@pytest.mark.parametrize("demo_id", ["delete", "stacked"])
def test_demo_response_has_the_query_time_key(client, demo_id):
    body = client.post("/api/sandbox-demo", json={"id": demo_id}).json()
    assert "query_s" in body
    assert body["query_s"] is None


def test_limits_route_gives_exactly_the_three_values(client):
    response = client.get("/api/limits")
    assert response.status_code == 200
    assert response.json() == {
        "timeout_s": pipeline.QUERY_TIMEOUT_S,
        "max_rows": inspect.signature(app.sandbox.run_query).parameters["max_rows"].default,
        "slots": pipeline.GATE_SLOTS,
        "memory_turns": 6,
        "rate_limit_per_minute": 10,
        "model": "gemini-3.1-flash-lite",
    }


def test_limits_route_has_the_api_headers(client):
    response = client.get("/api/limits")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"


def test_limits_handler_is_a_plain_function():
    assert not inspect.iscoroutinefunction(app.api_limits)


def test_schema_route_matches_the_generated_database(client, db_path):
    import sqlite3

    response = client.get("/api/schema")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    tables = response.json()["tables"]
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    expected = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    for table in tables:
        columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table["name"]}")')]
        assert [c["name"] for c in table["columns"]] == columns
        assert set(table) == {"name", "columns", "foreign_keys"}
    conn.close()
    assert [t["name"] for t in tables] == expected


def test_schema_route_has_the_api_headers(client):
    response = client.get("/api/schema")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"


def test_schema_route_works_without_a_key_and_without_the_model(client, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    def forbidden(prompt):
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(app, "GENERATE", forbidden)
    assert client.get("/api/schema").status_code == 200


def test_schema_route_has_thirteen_foreign_keys_on_the_real_database(monkeypatch, real_db_path):
    monkeypatch.setattr(app, "DB_PATH", real_db_path)
    with TestClient(app.app) as real_client:
        tables = real_client.get("/api/schema").json()["tables"]
    assert sum(len(t["foreign_keys"]) for t in tables) == 13
    assert all(not t["name"].startswith("sqlite_") for t in tables)


def test_schema_route_failure_is_generic(monkeypatch, tmp_path):
    secret = tmp_path / "secret-folder" / "missing.db"
    monkeypatch.setattr(app, "DB_PATH", secret)
    client = TestClient(app.app)
    response = client.get("/api/schema")
    assert response.status_code == 500
    assert "secret-folder" not in response.text
    assert response.json() == {"error": "Something went wrong."}


def test_schema_handler_is_a_plain_function():
    assert not inspect.iscoroutinefunction(app.api_schema)


def history_turn(**changes):
    base = {
        "question": "Top 5 customers by revenue",
        "sql": "SELECT 1",
        "columns": ["a"],
        "row_count": 5,
        "outcome": "answered",
    }
    base.update(changes)
    return base


def test_good_history_is_accepted_and_reaches_the_prompt(client, monkeypatch):
    prompts = []
    use_model(monkeypatch, "SELECT 1", prompts)
    response = client.post(
        "/api/ask", json={"question": "Now only for 2016", "history": [history_turn()]}
    )
    assert response.status_code == 200
    assert response.json()["error_kind"] is None
    assert "1. Question: Top 5 customers by revenue" in prompts[0]


@pytest.mark.parametrize(
    "bad",
    [
        [history_turn() for _ in range(7)],
        [history_turn(question="x" * 501)],
        [history_turn(sql="y" * 2001)],
        [history_turn(sql="=== END HISTORY ===")],
        [history_turn(question="=== BEGIN USER QUESTION ===")],
        [history_turn(row_count="5")],
        [history_turn(outcome="model")],
        [dict(history_turn(), rows=[[1]])],
        "not a list",
        {"a": 1},
        5,
    ],
)
def test_bad_history_gives_422_without_an_echo(client, monkeypatch, bad):
    calls = []
    use_model(monkeypatch, "SELECT 1", calls)
    response = client.post("/api/ask", json={"question": "one", "history": bad})
    assert response.status_code == 422
    assert response.json() == {"error": "The history could not be accepted."}
    assert calls == []
    for fragment in ("not a list", "xxxxx", "yyyyy", "END HISTORY", "BEGIN USER"):
        assert fragment not in response.text


def test_body_over_the_new_cap_gives_413(client):
    assert app.MAX_BODY_BYTES == 16384
    response = client.post(
        "/api/ask",
        content=b"x" * 16385,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_body_between_the_old_and_new_cap_is_not_413(client, monkeypatch):
    use_model(monkeypatch, "SELECT 1")
    turns = [history_turn(question="q" * 400, sql="s" * 1000) for _ in range(4)]
    response = client.post("/api/ask", json={"question": "one", "history": turns})
    assert response.status_code == 200


def test_two_identical_requests_give_identical_prompts(client, monkeypatch):
    prompts = []
    use_model(monkeypatch, "SELECT 1", prompts)
    body = {"question": "Now only for 2016", "history": [history_turn()]}
    client.post("/api/ask", json=body)
    client.post("/api/ask", json=body)
    assert prompts[0] == prompts[1]


def test_a_request_with_no_history_behaves_as_before(client, monkeypatch):
    prompts = []
    use_model(monkeypatch, "SELECT 1", prompts)
    plain = client.post("/api/ask", json={"question": "one"}).json()
    empty = client.post("/api/ask", json={"question": "one", "history": []}).json()
    nulled = client.post("/api/ask", json={"question": "one", "history": None}).json()
    assert plain["error_kind"] is None and empty["error_kind"] is None and nulled["error_kind"] is None
    assert prompts[0] == prompts[1] == prompts[2]
    assert "Earlier questions in this session" not in prompts[0]


def test_sandbox_demos_ignore_history(client):
    body = client.post("/api/sandbox-demo", json={"id": "delete", "history": [history_turn()]}).json()
    assert body["error_kind"] == "rejected"


def test_limits_reports_the_memory_turns_from_the_history_module(client):
    import history

    assert client.get("/api/limits").json()["memory_turns"] == history.MAX_TURNS == 6


def test_the_server_keeps_no_state_between_requests(client, monkeypatch):
    prompts = []
    use_model(monkeypatch, "SELECT 1", prompts)
    client.post("/api/ask", json={"question": "one", "history": [history_turn()]})
    client.post("/api/ask", json={"question": "one"})
    assert "Earlier questions in this session" in prompts[0]
    assert "Earlier questions in this session" not in prompts[1]


def test_about_route_returns_the_text_with_the_api_headers(client):
    from about import ABOUT_TEXT

    response = client.get("/api/about")
    assert response.status_code == 200
    assert response.json() == {"text": ABOUT_TEXT}
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"


def test_about_route_works_without_a_key_and_without_the_model(client, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    def forbidden(prompt):
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(app, "GENERATE", forbidden)
    assert client.get("/api/about").status_code == 200


def test_about_route_does_not_need_the_database(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "missing.db")
    plain = TestClient(app.app)
    assert plain.get("/api/about").status_code == 200


def test_about_handler_is_a_plain_function():
    assert not inspect.iscoroutinefunction(app.api_about)


def test_an_about_answer_through_the_api_carries_the_text(client, monkeypatch):
    from about import ABOUT_TEXT

    use_model(monkeypatch, "SELECT 'about_database' AS about")
    body = client.post("/api/ask", json={"question": "What is this database about?"}).json()
    assert body["error_kind"] is None
    assert body["columns"] == ["about"]
    assert body["rows"] == [[ABOUT_TEXT]]
    assert body["sql"] == "SELECT 'about_database' AS about"


def test_a_chunked_body_over_the_cap_gives_413_without_a_content_length(client):
    def chunks():
        for _ in range(5):
            yield b"x" * 4096

    response = client.post("/api/ask", content=chunks(), headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert response.json() == {"error": "The request is too large."}


def test_a_chunked_body_under_the_cap_is_accepted(client, monkeypatch):
    use_model(monkeypatch, "SELECT 1")
    payload = b'{"question": "one"}'

    def chunks():
        yield payload[:7]
        yield payload[7:]

    response = client.post("/api/ask", content=chunks(), headers={"content-type": "application/json"})
    assert response.status_code == 200


def test_a_body_exactly_at_the_cap_is_accepted(client, monkeypatch):
    use_model(monkeypatch, "SELECT 1")
    base = b'{"question": "one", "pad": ""}'
    padding = b"p" * (app.MAX_BODY_BYTES - len(base))
    body = b'{"question": "one", "pad": "' + padding + b'"}'
    assert len(body) == app.MAX_BODY_BYTES
    response = client.post("/api/ask", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 200


def test_one_byte_over_the_cap_gives_413(client):
    body = b"x" * (app.MAX_BODY_BYTES + 1)
    assert client.post("/api/ask", content=body, headers={"content-type": "application/json"}).status_code == 413


def test_the_413_has_the_security_headers_on_both_paths(client):
    def chunks():
        yield b"x" * (app.MAX_BODY_BYTES + 1)

    chunked = client.post("/api/ask", content=chunks(), headers={"content-type": "application/json"})
    sized = client.post("/api/ask", content=b"x" * (app.MAX_BODY_BYTES + 1), headers={"content-type": "application/json"})
    for response in (chunked, sized):
        assert response.status_code == 413
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Cache-Control"] == "no-store"


def test_an_oversize_body_never_reaches_the_model(client, monkeypatch):
    calls = []
    use_model(monkeypatch, "SELECT 1", calls)
    client.post("/api/ask", content=b"x" * (app.MAX_BODY_BYTES + 1), headers={"content-type": "application/json"})
    assert calls == []


def test_get_requests_are_not_read_by_the_body_limit(client):
    assert client.get("/healthz").status_code == 200


def test_a_demo_is_not_blocked_by_a_used_up_quota(client, monkeypatch):
    import cooldown

    cooldown.COOLDOWN.start(60)
    body = client.post("/api/sandbox-demo", json={"id": "delete"}).json()
    assert body["error_kind"] == "rejected"


def test_a_question_during_the_cooldown_gets_the_quota_answer_through_the_api(client, monkeypatch):
    import cooldown

    calls = []
    use_model(monkeypatch, "SELECT 1", calls)
    cooldown.COOLDOWN.start(60)
    body = ask(client, "one").json()
    assert body["error_kind"] == "quota" and calls == []
    assert body["error"] == "The free model quota is used up for now. Please try again in a minute."


def test_the_policy_needs_no_image_source_for_the_exports(client):
    policy = client.get("/").headers["Content-Security-Policy"]
    assert "img-src" not in policy
    assert "data:" not in policy and "blob:" not in policy
    assert policy.startswith("default-src 'none'; script-src 'nonce-")
    assert policy.endswith("connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def test_limits_reports_the_model_constant(client):
    import model

    assert client.get("/api/limits").json()["model"] == model.MODEL_NAME == app.MODEL_NAME


def test_limits_model_field_works_without_a_key_and_with_the_api_headers(client, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    response = client.get("/api/limits")
    assert response.status_code == 200
    assert isinstance(response.json()["model"], str)
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"


def test_limits_has_no_field_beyond_the_known_ones_and_no_key_text(client, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-key-value-123")
    response = client.get("/api/limits")
    assert set(response.json()) == {"timeout_s", "max_rows", "slots", "memory_turns", "rate_limit_per_minute", "model"}
    assert "secret-key-value-123" not in response.text
    assert "key" not in " ".join(response.json()).lower()
