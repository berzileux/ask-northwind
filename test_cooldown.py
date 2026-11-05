import threading

import pytest

import pipeline
from conftest import file_hash
from cooldown import COOLDOWN_S, QuotaCooldown
from model import ModelError


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture(scope="module", autouse=True)
def source_unchanged(db_path):
    before = file_hash(db_path)
    yield
    assert file_hash(db_path) == before


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(pipeline, "COOLDOWN", QuotaCooldown(fake))
    return fake


def failing(kind):
    calls = []

    def generate(prompt):
        calls.append(prompt)
        raise ModelError("failed", kind)

    generate.calls = calls
    return generate


def working():
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return "SELECT 1"

    generate.calls = calls
    return generate


def test_the_cooldown_is_sixty_seconds():
    assert COOLDOWN_S == 60.0


def test_the_class_counts_down_with_an_injected_clock():
    clock = FakeClock()
    cooldown = QuotaCooldown(clock)
    assert not cooldown.active() and cooldown.remaining() == 0.0
    cooldown.start(60)
    assert cooldown.active() and cooldown.remaining() == 60.0
    clock.now += 25
    assert cooldown.remaining() == 35.0
    clock.now += 35
    assert not cooldown.active() and cooldown.remaining() == 0.0


def test_a_shorter_start_does_not_shorten_a_longer_cooldown():
    clock = FakeClock()
    cooldown = QuotaCooldown(clock)
    cooldown.start(60)
    cooldown.start(5)
    assert cooldown.remaining() == 60.0


def test_reset_clears_the_cooldown():
    cooldown = QuotaCooldown(FakeClock())
    cooldown.start(60)
    cooldown.reset()
    assert not cooldown.active()


def test_a_quota_error_starts_the_cooldown_and_gives_the_quota_answer(db_path, clock):
    generate = failing("quota")
    answer = pipeline.ask("How many customers are there?", db_path, generate)
    assert answer.error_kind == "quota"
    assert answer.error == pipeline.MESSAGES["quota"]
    assert pipeline.COOLDOWN.active()
    assert len(generate.calls) == 1


def test_the_second_ask_inside_the_window_does_not_call_the_model(db_path, clock):
    first = failing("quota")
    pipeline.ask("one", db_path, first)
    second = working()
    answer = pipeline.ask("two", db_path, second)
    assert answer.error_kind == "quota" and second.calls == []
    assert answer.rows == [] and answer.query_s is None
    assert answer.elapsed_s < 0.5


def test_after_the_window_the_model_is_called_again(db_path, clock):
    pipeline.ask("one", db_path, failing("quota"))
    clock.now += COOLDOWN_S + 1
    again = working()
    answer = pipeline.ask("two", db_path, again)
    assert answer.error_kind is None and len(again.calls) == 1


def test_an_unavailable_error_does_not_start_the_cooldown(db_path, clock):
    answer = pipeline.ask("one", db_path, failing("unavailable"))
    assert answer.error_kind == "unavailable"
    assert answer.error == pipeline.MESSAGES["unavailable"]
    assert not pipeline.COOLDOWN.active()
    again = working()
    assert pipeline.ask("two", db_path, again).error_kind is None and len(again.calls) == 1


def test_a_plain_model_error_does_not_start_the_cooldown(db_path, clock):
    answer = pipeline.ask("one", db_path, failing("model"))
    assert answer.error_kind == "model" and not pipeline.COOLDOWN.active()


def test_the_cooldown_takes_no_gate_slot_and_runs_no_sandbox(db_path, clock, monkeypatch):
    pipeline.ask("one", db_path, failing("quota"))
    for _ in range(pipeline.GATE_SLOTS):
        pipeline.GATE.acquire()
    monkeypatch.setattr(pipeline, "run_query", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sandbox used")))
    try:
        answer = pipeline.ask("two", db_path, working())
    finally:
        for _ in range(pipeline.GATE_SLOTS):
            pipeline.GATE.release()
    assert answer.error_kind == "quota"


def test_concurrent_asks_during_the_window_all_get_the_quota_answer(db_path, clock):
    pipeline.ask("one", db_path, failing("quota"))
    generate = working()
    kinds = []

    def worker():
        kinds.append(pipeline.ask("again", db_path, generate).error_kind)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert kinds == ["quota"] * 8 and generate.calls == []


def test_a_bad_question_is_still_an_input_error_during_the_cooldown(db_path, clock):
    pipeline.ask("one", db_path, failing("quota"))
    assert pipeline.ask("", db_path, working()).error_kind == "input"


def test_use_cooldown_false_ignores_and_never_starts_it(db_path, clock):
    pipeline.ask("one", db_path, failing("quota"))
    generate = working()
    assert pipeline.ask("two", db_path, generate, use_cooldown=False).error_kind is None
    pipeline.COOLDOWN.reset()
    assert pipeline.ask("three", db_path, failing("quota"), use_cooldown=False).error_kind == "quota"
    assert not pipeline.COOLDOWN.active()


def test_the_visitor_messages_are_generic():
    assert pipeline.MESSAGES["quota"] == "The free model quota is used up for now. Please try again in a minute."
    assert pipeline.MESSAGES["unavailable"] == "The model service is busy or slow right now. Please try again."
    assert pipeline.MESSAGES["model"] == "The model could not produce a query."
