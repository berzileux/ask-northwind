import subprocess
import sys

import pytest

import model
from conftest import file_hash


@pytest.fixture(scope="module", autouse=True)
def source_unchanged(db_path):
    before = file_hash(db_path)
    yield
    assert file_hash(db_path) == before


def test_the_name_is_a_module_level_string_constant():
    assert isinstance(model.MODEL_NAME, str)
    assert model.MODEL_NAME == "gemini-3.1-flash-lite"


def test_the_call_uses_the_same_constant(monkeypatch):
    import types

    seen = {}

    class FakeModels:
        def generate_content(self, model, contents, config):
            seen["model"] = model

            class Response:
                text = "SELECT 1"

            return Response()

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = FakeModels()

    # Fake SDK modules, removed again by monkeypatch, so the real SDK is never loaded here.
    google = types.ModuleType("google")
    genai = types.ModuleType("google.genai")
    fake_types = types.ModuleType("google.genai.types")
    genai.Client = FakeClient
    genai.types = fake_types
    fake_types.HttpOptions = lambda **kwargs: kwargs
    fake_types.GenerateContentConfig = lambda **kwargs: kwargs
    google.genai = genai
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    monkeypatch.setattr(model, "MODEL_NAME", "some-other-name")
    assert model.generate_sql("q") == "SELECT 1"
    assert seen["model"] == "some-other-name"


def test_the_call_reads_the_constant_at_call_time_not_a_copy():
    source = open(model.__file__).read()
    assert "model=MODEL_NAME" in source
    assert source.count('"gemini') == 1


def test_there_is_no_environment_override():
    source = open(model.__file__).read()
    assert "MODEL_NAME = os.environ" not in source
    assert "environ.get(\"MODEL" not in source


CHECK = (
    "import re, sys\n"
    "src = open('model.py').read()\n"
    "name = sys.argv[1]\n"
    "src = src.replace('MODEL_NAME = \"gemini-3.1-flash-lite\"', 'MODEL_NAME = ' + repr(name), 1)\n"
    "try:\n"
    "    exec(compile(src, 'model.py', 'exec'), {'__name__': 'model_under_test'})\n"
    "    print('ACCEPTED')\n"
    "except ValueError as exc:\n"
    "    print('REFUSED ' + str(exc))\n"
)


def run_with_name(name):
    done = subprocess.run(
        [sys.executable, "-c", CHECK, name], capture_output=True, text=True, timeout=60, cwd=str(model.__file__).rsplit("/", 1)[0]
    )
    assert done.returncode == 0, done.stderr[-1500:]
    return done.stdout.strip()


@pytest.mark.parametrize("name", ["gemini-3.1-flash-lite", "a", "x" * 64, "models/gemini_2.5-pro", "A.b_c/d-9"])
def test_valid_names_are_accepted(name):
    assert run_with_name(name) == "ACCEPTED"


@pytest.mark.parametrize(
    "name", ["", "two words", 'quote"d', "it's", "x" * 65, "semi;colon", "line\nbreak", "tag<b>", "caf" + chr(233)]
)
def test_bad_names_stop_the_app_at_import_with_a_clear_message(name):
    out = run_with_name(name)
    assert out.startswith("REFUSED MODEL_NAME must be 1 to 64 characters")


def test_the_real_name_passes_the_rule():
    import re

    assert re.fullmatch(r"[A-Za-z0-9._/-]{1,64}", model.MODEL_NAME)


def test_importing_model_does_not_import_the_sdk_or_need_the_key():
    code = (
        "import os, sys\n"
        "os.environ.pop('GEMINI_API_KEY', None)\n"
        "import model\n"
        "print(model.MODEL_NAME)\n"
        "print(any(m == 'google' or m.startswith('google.') for m in sys.modules))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=str(model.__file__).rsplit("/", 1)[0]
    )
    assert done.returncode == 0, done.stderr[-1500:]
    lines = done.stdout.split()
    assert lines == ["gemini-3.1-flash-lite", "False"]
