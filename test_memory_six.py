import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app
import history
import prompt
from history import MAX_TURNS, clean_history

ROOT = Path(__file__).parent

# Hashes of two prompts built before the turn count and the total cap changed, from fixed
# text so they do not depend on the database: the prompts with no history and with four turns.
SCHEMA = "Table T\n  a TEXT"
DEFINITIONS = "defs"
NO_HISTORY_SHA256 = "5ab8eb7d194af4a9863543c065dec8fe214ddec33da5e4c7d320d09b6ffa69c5"
FOUR_TURN_SHA256 = "ecf9d9849adabd00406d1df1e39d7022a88b1142f075d84122a3cee831762083"


def turn(i, **changes):
    base = {
        "question": f"Question number {i}",
        "sql": f"SELECT {i}",
        "columns": ["a", "b"],
        "row_count": i,
        "outcome": "answered",
    }
    base.update(changes)
    return base


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def test_the_new_constants():
    assert history.MAX_TURNS == 6 and history.MAX_TOTAL_CHARS == 9000
    assert (history.MAX_QUESTION_CHARS, history.MAX_SQL_CHARS, history.MAX_COLUMNS, history.MAX_COLUMN_CHARS) == (500, 2000, 30, 64)


def test_six_turns_are_all_kept_and_a_seventh_is_refused():
    turns = [turn(i) for i in range(1, 7)]
    assert clean_history(turns) == turns
    with pytest.raises(ValueError):
        clean_history([turn(i) for i in range(1, 8)])


def test_the_total_cap_refuses_long_turns_and_accepts_them_just_under_it():
    # 6 turns of 1500 characters each is exactly 9000: accepted; one more character is refused.
    exact = [turn(i, question="q" * 500, sql="s" * 1000) for i in range(6)]
    assert sum(len(t["question"]) + len(t["sql"]) + sum(len(c) for c in t["columns"]) for t in exact) == 6 * 1502
    smaller = [turn(i, question="q" * 500, sql="s" * 999, columns=["c"]) for i in range(6)]
    assert sum(len(t["question"]) + len(t["sql"]) + 1 for t in smaller) == 9000
    assert clean_history(smaller) == smaller
    with pytest.raises(ValueError):
        clean_history(exact)


def test_the_server_refuses_instead_of_cutting_and_never_drops_a_turn_silently():
    long_turns = [turn(i, question="q" * 500, sql="s" * 1500) for i in range(6)]
    with pytest.raises(ValueError):
        clean_history(long_turns)


def test_the_size_check_leaves_room_under_the_body_cap():
    assert history.MAX_TOTAL_CHARS + history.MAX_QUESTION_CHARS + history.BODY_OVERHEAD_MARGIN < history.REQUEST_BODY_CAP
    assert history.REQUEST_BODY_CAP == app.MAX_BODY_BYTES == 16384


def test_a_larger_total_cap_fails_loudly_at_import():
    source = (ROOT / "history.py").read_text().replace("MAX_TOTAL_CHARS = 9000", "MAX_TOTAL_CHARS = 14000")
    done = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, timeout=30)
    assert done.returncode != 0 and "history does not fit the body cap" in done.stderr


def test_a_larger_turn_budget_expressed_through_the_total_also_fails_loudly():
    source = (ROOT / "history.py").read_text().replace("BODY_OVERHEAD_MARGIN = 2000", "BODY_OVERHEAD_MARGIN = 9000")
    done = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, timeout=30)
    assert done.returncode != 0 and "history does not fit the body cap" in done.stderr


@pytest.mark.parametrize("position", range(6))
@pytest.mark.parametrize(
    "marker", ["=== BEGIN HISTORY ===", "end user question", "===  begin x", "BEGIN\tUSER   QUESTION"]
)
def test_markers_are_refused_in_any_of_the_six_turns(position, marker):
    turns = [turn(i) for i in range(6)]
    turns[position]["sql"] = f"SELECT 1 {marker}"
    with pytest.raises(ValueError):
        clean_history(turns)


def test_rows_are_never_accepted_in_any_turn():
    for position in range(6):
        turns = [turn(i) for i in range(6)]
        turns[position]["rows"] = [[1, 2]]
        with pytest.raises(ValueError):
            clean_history(turns)


@pytest.fixture
def client(monkeypatch, db_path):
    monkeypatch.setattr(app, "DB_PATH", db_path)
    with TestClient(app.app) as test_client:
        yield test_client


def test_a_body_with_six_turns_at_the_total_cap_is_accepted(client, monkeypatch):
    monkeypatch.setattr(app, "GENERATE", lambda p: "SELECT 1")
    turns = [turn(i, question="q" * 500, sql="s" * 999, columns=["c"]) for i in range(6)]
    response = client.post("/api/ask", json={"question": "x" * 500, "history": turns})
    assert response.status_code == 200 and response.json()["error_kind"] is None


def test_the_api_reports_six_turns(client):
    assert client.get("/api/limits").json()["memory_turns"] == 6


def test_a_seventh_turn_in_a_request_gives_422_without_an_echo(client):
    turns = [turn(i, question="SECRETQUESTION") for i in range(7)]
    response = client.post("/api/ask", json={"question": "one", "history": turns})
    assert response.status_code == 422 and "SECRETQUESTION" not in response.text


def test_a_body_that_would_pass_the_cap_still_gets_413(client):
    turns = [turn(i, question="q" * 500, sql="s" * 2000, columns=["c" * 64] * 30) for i in range(6)]
    big = client.post("/api/ask", json={"question": "x" * 500, "history": turns})
    assert big.status_code == 413
    huge = client.post("/api/ask", content=b"x" * 16385, headers={"content-type": "application/json"})
    assert huge.status_code == 413


def test_the_prompt_with_no_history_is_byte_identical_to_before():
    text = prompt.build_prompt("How many customers are there?", SCHEMA, DEFINITIONS, prompt.NOTES)
    assert sha(text) == NO_HISTORY_SHA256
    assert sha(prompt.build_prompt("How many customers are there?", SCHEMA, DEFINITIONS, prompt.NOTES, [])) == NO_HISTORY_SHA256


def test_the_prompt_with_four_turns_is_byte_identical_to_before():
    text = prompt.build_prompt("Follow up?", SCHEMA, DEFINITIONS, prompt.NOTES, [turn(i) for i in range(1, 5)])
    assert sha(text) == FOUR_TURN_SHA256


def test_only_five_or_six_turns_differ_from_the_four_turn_prompt():
    four = prompt.build_prompt("Follow up?", SCHEMA, DEFINITIONS, prompt.NOTES, [turn(i) for i in range(1, 5)])
    six = prompt.build_prompt("Follow up?", SCHEMA, DEFINITIONS, prompt.NOTES, [turn(i) for i in range(1, 7)])
    assert six != four
    assert "6. Question: Question number 6" in six and "5. Question: Question number 5" in six


def test_the_page_fallback_and_cap_follow_the_history_module():
    source = (ROOT / "index.html").read_text()
    assert "var windowSize = 6;" in source
    assert f"var MAX_SENT_CHARS = {history.MAX_TOTAL_CHARS};" in source
    assert "lim.memory_turns" in source
    assert "Memory: 0 of 6 questions" in source and "last 6 questions" in source
