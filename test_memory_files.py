import fnmatch
from pathlib import Path

import pytest

import prompt
from prompt import (
    QUESTION_END,
    QUESTION_START,
    RULES,
    build_prompt,
    load_notes,
    load_rules,
)

ROOT = Path(__file__).parent
RULES_FILE = ROOT / "base_rules.md"
NOTES_FILE = ROOT / "dataset_notes.md"
FILES = ["base_rules.md", "dataset_notes.md"]


def test_base_rules_file_limits():
    raw = RULES_FILE.read_bytes()
    raw.decode("ascii")
    assert 0 < len(raw) <= 8000
    text = raw.decode("ascii")
    assert "<<QUESTION_START>>" in text and "<<QUESTION_END>>" in text
    leftover = text.replace("<<QUESTION_START>>", "").replace("<<QUESTION_END>>", "")
    leftover = leftover.replace("<<HISTORY_START>>", "").replace("<<HISTORY_END>>", "")
    assert "<<" not in leftover and ">>" not in leftover


def test_dataset_notes_file_limits():
    raw = NOTES_FILE.read_bytes()
    raw.decode("ascii")
    assert 0 < len(raw) <= 2500


def test_rules_have_the_delimiter_values_and_no_tokens():
    assert QUESTION_START in RULES and QUESTION_END in RULES
    assert "<<" not in RULES and ">>" not in RULES


def test_rules_are_loaded_from_the_file_next_to_the_module():
    assert prompt.HERE == ROOT
    assert RULES == load_rules(ROOT)
    assert prompt.NOTES == load_notes(ROOT)


def test_missing_rules_file_raises(tmp_path):
    with pytest.raises(RuntimeError):
        load_rules(tmp_path)


def test_missing_notes_file_raises(tmp_path):
    with pytest.raises(RuntimeError):
        load_notes(tmp_path)


def test_empty_files_raise(tmp_path):
    (tmp_path / "base_rules.md").write_text("  \n")
    (tmp_path / "dataset_notes.md").write_text("")
    with pytest.raises(RuntimeError):
        load_rules(tmp_path)
    with pytest.raises(RuntimeError):
        load_notes(tmp_path)


def test_unknown_token_raises(tmp_path):
    (tmp_path / "base_rules.md").write_text("rule <<QUESTION_START>> <<OTHER>>\n")
    with pytest.raises(RuntimeError):
        load_rules(tmp_path)


def test_tokens_are_replaced(tmp_path):
    (tmp_path / "base_rules.md").write_text("between <<QUESTION_START>> and <<QUESTION_END>>\n")
    assert load_rules(tmp_path) == f"between {QUESTION_START} and {QUESTION_END}"


def test_notes_come_before_the_question_block():
    text = build_prompt("the question", "schema", "definitions", "a note")
    assert "Dataset notes:\na note" in text
    assert text.index("Definitions:") < text.index("a note")
    assert text.index("a note") < text.rindex(QUESTION_START)


def test_empty_notes_give_the_old_output():
    old = "\n\n".join(
        [
            RULES,
            "Schema:\nschema",
            "Definitions:\ndefinitions",
            f"{QUESTION_START}\nthe question\n{QUESTION_END}",
        ]
    )
    assert build_prompt("the question", "schema", "definitions") == old
    assert build_prompt("the question", "schema", "definitions", "") == old


def dockerignore_patterns():
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


@pytest.mark.parametrize("name", FILES)
def test_dockerignore_does_not_exclude_the_memory_files(name):
    for pattern in dockerignore_patterns():
        assert not fnmatch.fnmatch(name, pattern.rstrip("/")), pattern


def test_dockerfile_copies_the_whole_context():
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    copies = [line for line in lines if line.startswith("COPY ")]
    whole = [line for line in copies if line.rstrip().endswith(" . .")]
    named = [line for line in copies if all(name in line for name in FILES)]
    assert whole or named
