import pytest
from fastapi.testclient import TestClient

import app
import pipeline
from ratelimit import RateLimiter, client_key


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def limiter(clock, limit=3, window=60, max_keys=10000):
    return RateLimiter(limit, window, clock, max_keys)


def test_the_limit_then_the_refusal_with_a_wait_time():
    clock = FakeClock()
    rl = limiter(clock)
    assert [rl.allow("a")[0] for _ in range(3)] == [True, True, True]
    allowed, wait = rl.allow("a")
    assert allowed is False and 1 <= wait <= 61


def test_the_window_slides():
    clock = FakeClock()
    rl = limiter(clock)
    rl.allow("a")
    clock.now += 30
    rl.allow("a")
    rl.allow("a")
    assert rl.allow("a")[0] is False
    clock.now += 31
    assert rl.allow("a")[0] is True
    assert rl.allow("a")[0] is False


def test_the_wait_time_counts_down_to_the_oldest_hit():
    clock = FakeClock()
    rl = limiter(clock, limit=1)
    rl.allow("a")
    clock.now += 10
    _, wait = rl.allow("a")
    assert 50 <= wait <= 51


def test_different_keys_are_independent():
    clock = FakeClock()
    rl = limiter(clock, limit=1)
    assert rl.allow("a")[0] and rl.allow("b")[0]
    assert not rl.allow("a")[0] and not rl.allow("b")[0]


def test_a_refused_request_does_not_extend_the_window():
    clock = FakeClock()
    rl = limiter(clock, limit=1)
    rl.allow("a")
    for _ in range(5):
        clock.now += 5
        assert not rl.allow("a")[0]
    clock.now += 40
    assert rl.allow("a")[0]


def test_the_table_is_bounded_and_the_oldest_key_goes_first():
    clock = FakeClock()
    rl = limiter(clock, limit=5, max_keys=3)
    for key in ("a", "b", "c", "d"):
        rl.allow(key)
        clock.now += 1
    assert rl.size() == 3
    assert rl.allow("a")[0] is True
    assert rl.size() == 3


def test_expired_keys_are_dropped_on_access():
    clock = FakeClock()
    rl = limiter(clock, max_keys=100)
    for key in ("a", "b", "c"):
        rl.allow(key)
    clock.now += 61
    rl.allow("d")
    assert rl.size() == 1


def test_a_flood_of_distinct_keys_stays_at_the_bound():
    clock = FakeClock()
    rl = limiter(clock, max_keys=50)
    for i in range(500):
        rl.allow(f"key{i}")
    assert rl.size() <= 50


def test_reset_empties_the_table():
    rl = limiter(FakeClock())
    rl.allow("a")
    rl.reset()
    assert rl.size() == 0


@pytest.mark.parametrize(
    "header, host, expected",
    [
        ("203.0.113.5", "10.0.0.1", "203.0.113.5"),
        ("198.51.100.9, 203.0.113.5", "10.0.0.1", "203.0.113.5"),
        ("1.1.1.1, 198.51.100.9, 203.0.113.5", "10.0.0.1", "203.0.113.5"),
        ("203.0.113.5 ", "10.0.0.1", "203.0.113.5"),
        ("", "10.0.0.1", "10.0.0.1"),
        (None, "10.0.0.1", "10.0.0.1"),
        ("not-an-address", "10.0.0.1", "10.0.0.1"),
        ("203.0.113.5, ", "10.0.0.1", "10.0.0.1"),
        (",", "10.0.0.1", "10.0.0.1"),
        ("203.0.113.5, <script>", "10.0.0.1", "10.0.0.1"),
        ("2001:db8::1", "10.0.0.1", "2001:db8::1"),
        ("203.0.113.5", None, "203.0.113.5"),
        ("", None, "unknown"),
    ],
)
def test_client_key_from_the_proxy_header(header, host, expected):
    assert client_key(header, host) == expected


def test_a_client_written_left_entry_does_not_change_the_key():
    real = "203.0.113.5"
    keys = {client_key(f"{forged}, {real}", "10.0.0.1") for forged in ("1.1.1.1", "2.2.2.2", "9.9.9.9, 8.8.8.8", "evil")}
    assert keys == {real}


def test_the_hop_count_picks_the_entry_from_the_right():
    header = "1.1.1.1, 2.2.2.2, 3.3.3.3"
    assert client_key(header, "h", hops=1) == "3.3.3.3"
    assert client_key(header, "h", hops=2) == "2.2.2.2"
    assert client_key(header, "h", hops=3) == "1.1.1.1"
    assert client_key(header, "h", hops=4) == "h"
    assert client_key(header, "h", hops=0) == "h"


def test_the_module_says_the_hop_count_needs_confirming():
    text = open(__import__("ratelimit").__file__).read()
    assert "CONFIRM on the first real deployment" in text
    assert "TRUSTED_PROXY_HOPS = 1" in text


# Application level tests.
@pytest.fixture
def client(monkeypatch, db_path):
    monkeypatch.setattr(app, "DB_PATH", db_path)
    with TestClient(app.app) as test_client:
        yield test_client


def model_calls(monkeypatch):
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return "SELECT 1"

    monkeypatch.setattr(app, "GENERATE", generate)
    return calls


def ask(client, headers=None):
    return client.post("/api/ask", json={"question": "one"}, headers=headers or {})


def test_the_constants_and_their_place():
    assert app.RATE_LIMIT_REQUESTS == 10 and app.RATE_LIMIT_WINDOW_S == 60
    assert app.LIMITER.limit == 10 and app.LIMITER.window_s == 60


def test_the_eleventh_request_in_a_window_is_refused_with_retry_after(client, monkeypatch):
    calls = model_calls(monkeypatch)
    codes = [ask(client).status_code for _ in range(10)]
    assert codes == [200] * 10
    response = ask(client)
    assert response.status_code == 429
    wait = int(response.headers["Retry-After"])
    assert 1 <= wait <= 61
    body = response.json()
    assert body["error_kind"] == "rate_limited"
    assert body["error"] == f"You are asking too fast. Please wait {wait} seconds."
    assert body["retry_after_s"] == wait
    assert body["rows"] == [] and body["sql"] == ""
    assert len(calls) == 10


def test_a_limited_request_has_the_security_headers_and_calls_nothing(client, monkeypatch):
    calls = model_calls(monkeypatch)
    for _ in range(10):
        ask(client)
    before = len(calls)
    response = ask(client)
    assert response.status_code == 429
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"
    assert len(calls) == before


def test_a_limited_request_takes_no_gate_slot_and_runs_no_sandbox(client, monkeypatch):
    model_calls(monkeypatch)
    for _ in range(10):
        ask(client)
    for _ in range(pipeline.GATE_SLOTS):
        pipeline.GATE.acquire()
    monkeypatch.setattr(pipeline, "run_query", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sandbox used")))
    try:
        response = ask(client)
    finally:
        for _ in range(pipeline.GATE_SLOTS):
            pipeline.GATE.release()
    assert response.status_code == 429


def test_demos_and_asks_share_one_budget(client, monkeypatch):
    model_calls(monkeypatch)
    for _ in range(5):
        assert ask(client).status_code == 200
    for _ in range(5):
        assert client.post("/api/sandbox-demo", json={"id": "delete"}).status_code == 200
    assert ask(client).status_code == 429
    assert client.post("/api/sandbox-demo", json={"id": "delete"}).status_code == 429


def test_the_cheap_routes_are_never_limited(client):
    for _ in range(25):
        assert client.get("/").status_code == 200
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/limits").status_code == 200
        assert client.get("/api/schema").status_code == 200
        assert client.get("/api/about").status_code == 200
        assert client.get("/api/sandbox-demos").status_code == 200


def test_cheap_routes_do_not_use_up_the_budget(client, monkeypatch):
    model_calls(monkeypatch)
    for _ in range(30):
        client.get("/api/limits")
    assert ask(client).status_code == 200


def test_different_clients_have_separate_budgets(client, monkeypatch):
    model_calls(monkeypatch)
    for _ in range(10):
        ask(client, {"X-Forwarded-For": "203.0.113.1"})
    assert ask(client, {"X-Forwarded-For": "203.0.113.1"}).status_code == 429
    assert ask(client, {"X-Forwarded-For": "203.0.113.2"}).status_code == 200


def test_a_client_cannot_pick_its_own_key_with_a_left_entry(client, monkeypatch):
    model_calls(monkeypatch)
    for i in range(10):
        ask(client, {"X-Forwarded-For": f"10.9.9.{i}, 203.0.113.7"})
    assert ask(client, {"X-Forwarded-For": "10.1.1.1, 203.0.113.7"}).status_code == 429


def test_a_malformed_header_falls_back_to_the_connection_host(client, monkeypatch):
    model_calls(monkeypatch)
    for _ in range(10):
        ask(client, {"X-Forwarded-For": "garbage"})
    assert ask(client).status_code == 429


def test_the_window_slides_through_the_app(client, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(app, "LIMITER", RateLimiter(10, 60, clock))
    model_calls(monkeypatch)
    for _ in range(10):
        ask(client)
    assert ask(client).status_code == 429
    clock.now += 61
    assert ask(client).status_code == 200


def test_the_limits_route_reports_the_rate(client):
    assert client.get("/api/limits").json()["rate_limit_per_minute"] == app.RATE_LIMIT_REQUESTS == 10


def test_nothing_about_the_client_is_logged(client, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    model_calls(monkeypatch)
    for _ in range(11):
        ask(client, {"X-Forwarded-For": "203.0.113.99"})
    assert "203.0.113.99" not in caplog.text
    assert "testclient" not in caplog.text
