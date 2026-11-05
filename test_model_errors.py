import subprocess
import sys

import pytest

import pipeline
from conftest import file_hash
from model import ModelError, classify_error


class APIError(Exception):
    def __init__(self, code, text="provider detail with a secret"):
        super().__init__(f"{code} {text}")
        self.code = code


class ClientError(APIError):
    pass


class ServerError(APIError):
    pass


class TimeoutException(Exception):
    pass


class TransportError(Exception):
    pass


class ConnectError(TransportError):
    pass


@pytest.mark.parametrize(
    "exc, kind",
    [
        (ClientError(429), "quota"),
        (ServerError(500), "unavailable"),
        (ServerError(502), "unavailable"),
        (ServerError(503), "unavailable"),
        (ServerError(504), "unavailable"),
        (ClientError(400), "model"),
        (ClientError(401), "model"),
        (ClientError(403), "model"),
        (ClientError(404), "model"),
        (ServerError(501), "model"),
        (TimeoutError("slow"), "unavailable"),
        (ConnectionError("down"), "unavailable"),
        (TimeoutException("slow"), "unavailable"),
        (ConnectError("down"), "unavailable"),
        (ValueError("model returned no text"), "model"),
        (RuntimeError("anything"), "model"),
    ],
)
def test_classification_uses_the_class_and_status_code(exc, kind):
    assert classify_error(exc) == kind


def test_the_message_text_is_never_read():
    assert classify_error(ClientError(400, "quota 429 RESOURCE_EXHAUSTED")) == "model"
    assert classify_error(ValueError("429 quota exhausted 503 504")) == "model"
    assert classify_error(ServerError(503, "no text at all")) == "unavailable"


def test_a_bare_code_attribute_without_an_api_error_class_is_not_trusted():
    class Other(Exception):
        code = 429

    assert classify_error(Other()) == "model"


def test_a_boolean_code_is_not_a_status():
    class Odd(APIError):
        pass

    assert classify_error(Odd(True)) == "model"


def test_the_real_sdk_classes_are_classified_in_a_separate_process():
    # The SDK is loaded in a child process so this process stays free of it.
    code = (
        "from google.genai import errors\n"
        "import httpx\n"
        "from model import classify_error\n"
        "def make(cls, status):\n"
        "    return cls(status, {'error': {'code': status, 'message': 'x', 'status': 'S'}})\n"
        "out = [classify_error(make(errors.ClientError, 429)),\n"
        "       classify_error(make(errors.ServerError, 503)),\n"
        "       classify_error(make(errors.ServerError, 504)),\n"
        "       classify_error(make(errors.ClientError, 400)),\n"
        "       classify_error(httpx.ReadTimeout('t')),\n"
        "       classify_error(httpx.ConnectError('c'))]\n"
        "print(','.join(out))\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    if "No module named 'google'" in done.stderr:
        pytest.skip("the SDK is not installed")
    assert done.returncode == 0, done.stderr[-800:]
    assert done.stdout.strip() == "quota,unavailable,unavailable,model,unavailable,unavailable"


def test_model_error_defaults_and_rejects_unknown_kinds():
    assert ModelError("x").kind == "model"
    assert ModelError("x", "quota").kind == "quota"
    assert ModelError("x", "unavailable").kind == "unavailable"
    assert ModelError("x", "something else").kind == "model"


@pytest.fixture(scope="module", autouse=True)
def source_unchanged(db_path):
    before = file_hash(db_path)
    yield
    assert file_hash(db_path) == before


@pytest.mark.parametrize(
    "kind, message",
    [
        ("quota", "The free model quota is used up for now. Please try again in a minute."),
        ("unavailable", "The model service is busy or slow right now. Please try again."),
        ("model", "The model could not produce a query."),
    ],
)
def test_pipeline_maps_each_kind_to_its_own_generic_message(db_path, kind, message):
    def generate(prompt):
        raise ModelError("secret provider text SECRETKEY", kind)

    answer = pipeline.ask("one", db_path, generate)
    assert answer.error_kind == kind and answer.error == message
    assert "SECRETKEY" not in repr(answer) and "provider" not in answer.error.lower()


def test_a_plain_exception_from_the_model_is_the_generic_model_kind(db_path):
    def generate(prompt):
        raise RuntimeError("boom")

    assert pipeline.ask("one", db_path, generate).error_kind == "model"


def test_an_unusable_reply_is_the_generic_model_kind(db_path):
    assert pipeline.ask("one", db_path, lambda p: None).error_kind == "model"
    assert pipeline.ask("one", db_path, lambda p: "x" * 6000).error_kind == "model"


def test_the_failure_log_holds_the_class_name_and_no_request_text(db_path, caplog):
    import logging

    caplog.set_level(logging.DEBUG)

    def generate(prompt):
        raise ModelError("failed", "unavailable")

    pipeline.ask("QUESTIONTEXT how many customers", db_path, generate)
    failed = [r.getMessage() for r in caplog.records if "model failed" in r.getMessage()]
    assert failed and "ModelError" in failed[0]
    assert "GEMINI" not in " ".join(failed)
