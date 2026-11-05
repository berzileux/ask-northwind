import inspect
import threading

import pytest

import pipeline
import sandbox
from conftest import file_hash
from model import ModelError
from pipeline import ask

FOUR_WAY_CROSS_JOIN = (
    "SELECT count(*) FROM OrderDetails a, OrderDetails b, "
    "OrderDetails c, OrderDetails d"
)


@pytest.fixture(scope="module", autouse=True)
def source_unchanged(db_path):
    before = file_hash(db_path)
    yield
    assert file_hash(db_path) == before


def fake(text):
    return lambda prompt: text


def take_all_slots():
    for _ in range(pipeline.GATE_SLOTS):
        pipeline.GATE.acquire()


def release_all_slots():
    for _ in range(pipeline.GATE_SLOTS):
        pipeline.GATE.release()


def gate_is_free():
    """Acquire and release every slot without waiting; True if none is leaked."""
    taken = 0
    try:
        for _ in range(pipeline.GATE_SLOTS):
            if not pipeline.GATE.acquire(blocking=False):
                return False
            taken += 1
        return True
    finally:
        for _ in range(taken):
            pipeline.GATE.release()


def test_full_gate_gives_busy_without_leaking(db_path, monkeypatch):
    monkeypatch.setattr(pipeline, "WAIT_FOR_SLOT_S", 0.2)
    take_all_slots()
    try:
        answer = ask("one", db_path, fake("SELECT 1"))
        assert answer.error_kind == "busy"
        assert answer.rows == []
        assert answer.error == "The service is busy, please try again in a moment."
    finally:
        release_all_slots()
    assert gate_is_free()


def test_same_question_succeeds_after_slots_are_released(db_path, monkeypatch):
    monkeypatch.setattr(pipeline, "WAIT_FOR_SLOT_S", 0.2)
    take_all_slots()
    try:
        assert ask("one", db_path, fake("SELECT 1")).error_kind == "busy"
    finally:
        release_all_slots()

    answer = ask("one", db_path, fake("SELECT 1"))

    assert answer.error is None
    assert answer.rows == [(1,)]


def test_slot_is_released_when_the_query_is_rejected(db_path):
    answer = ask("one", db_path, fake("DELETE FROM Customers"))
    assert answer.error_kind == "rejected"
    assert gate_is_free()


def test_two_threads_both_succeed_and_stay_independent(db_path):
    answers = {}

    def worker(name, sql):
        answers[name] = ask(name, db_path, fake(sql))

    threads = [
        threading.Thread(target=worker, args=("first", "SELECT 1 AS a")),
        threading.Thread(target=worker, args=("second", "SELECT 2 AS b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert answers["first"].error is None and answers["second"].error is None
    assert answers["first"].columns == ["a"] and answers["first"].rows == [(1,)]
    assert answers["second"].columns == ["b"] and answers["second"].rows == [(2,)]


def test_waiting_for_a_slot_does_not_count_against_the_query_limit(db_path):
    # The wait (1.0 s) is longer than the query time limit (0.5 s), so a
    # timeout here would mean the wait was being counted.
    take_all_slots()
    timer = threading.Timer(1.0, release_all_slots)
    timer.start()
    try:
        answer = ask("one", db_path, fake("SELECT 1"), timeout_s=0.5)
    finally:
        timer.join()

    assert answer.error is None
    assert answer.rows == [(1,)]


def test_default_timeout_is_the_pipeline_constant():
    default = inspect.signature(ask).parameters["timeout_s"].default
    assert default == pipeline.QUERY_TIMEOUT_S


def test_progress_steps_is_a_positive_int_read_at_call_time(db_path, monkeypatch):
    assert isinstance(sandbox.PROGRESS_STEPS, int)
    assert sandbox.PROGRESS_STEPS > 0

    monkeypatch.setattr(sandbox, "PROGRESS_STEPS", 500)

    assert sandbox.run_query("SELECT 1", db_path).rows == [(1,)]


def raise_model_error(prompt):
    raise ModelError("boom")


def test_each_error_kind_is_produced(db_path, monkeypatch):
    assert ask("", db_path, fake("SELECT 1")).error_kind == "input"
    assert ask("one", db_path, raise_model_error).error_kind == "model"
    assert ask("one", db_path, fake("DROP TABLE Customers")).error_kind == "rejected"
    slow = ask("one", db_path, fake(FOUR_WAY_CROSS_JOIN), timeout_s=0.5)
    assert slow.error_kind == "timeout"

    monkeypatch.setattr(pipeline, "WAIT_FOR_SLOT_S", 0.2)
    take_all_slots()
    try:
        assert ask("one", db_path, fake("SELECT 1")).error_kind == "busy"
    finally:
        release_all_slots()
