import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from about import about_result, is_about_sql
from cooldown import COOLDOWN, COOLDOWN_S
from history import clean_history, contains_marker
from model import ModelError, generate_sql
from prompt import NOTES, build_prompt
from sandbox import SandboxError, SandboxTimeout, run_query
from schema import schema_text

log = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 500
MAX_SQL_CHARS = 5000
DEFINITIONS_PATH = Path(__file__).parent / "definitions.txt"

# Queries run one at a time because concurrent queries in one process slow
# each other down and would time out. A request waits for its turn up to
# WAIT_FOR_SLOT_S; that wait is outside the query time limit.
GATE_SLOTS = 1
WAIT_FOR_SLOT_S = 8.0
QUERY_TIMEOUT_S = 5.0
GATE = threading.BoundedSemaphore(GATE_SLOTS)

# Messages returned to callers are generic; detail goes to the log only.
MESSAGES = {
    "input": "The question must be 1 to 500 characters of plain text.",
    "model": "The model could not produce a query.",
    "rejected": "The query was not allowed or could not be run.",
    "timeout": "The query took too long.",
    "busy": "The service is busy, please try again in a moment.",
    "quota": "The free model quota is used up for now. Please try again in a minute.",
    "unavailable": "The model service is busy or slow right now. Please try again.",
}

_FENCE = re.compile(r"```[A-Za-z]*\s*\n?(.*?)\s*```", re.DOTALL)


@dataclass
class Answer:
    question: object
    sql: str
    columns: list
    rows: list
    truncated: bool
    elapsed_s: float
    error: object
    error_kind: object
    # Time spent inside the sandbox only, so the page can compare it with the query limit.
    query_s: object = None


def _valid_question(question):
    if not isinstance(question, str):
        return False
    if not 1 <= len(question.strip()) <= MAX_QUESTION_CHARS:
        return False
    return all(
        ch in "\n\t" or unicodedata.category(ch) != "Cc" for ch in question
    )


def _mentions_delimiter(question):
    # The prompt marks the question with begin and end lines, so a question
    # that repeats those words could fake the end of its own block.
    return contains_marker(question)


def _clean_sql(text):
    text = text.strip()
    fenced = _FENCE.fullmatch(text)
    return fenced.group(1) if fenced else text


def _generate_and_run(question, db_path, generate, timeout_s, history=None, use_cooldown=True):
    """Return (error_kind, sql, result); error_kind is None on success."""
    # While the quota is used up nothing is called: no prompt, no model, no gate, no sandbox.
    if use_cooldown and COOLDOWN.active():
        return "quota", "", None
    prompt = build_prompt(
        question,
        schema_text(db_path),
        DEFINITIONS_PATH.read_text().strip(),
        NOTES,
        history,
    )
    try:
        text = generate(prompt)
    except Exception as exc:
        log.warning("model failed: %s: %s", type(exc).__name__, exc)
        failure = exc.kind if isinstance(exc, ModelError) else "model"
        if failure == "quota" and use_cooldown:
            COOLDOWN.start(COOLDOWN_S)
        return failure, "", None
    if not isinstance(text, str) or len(text) > MAX_SQL_CHARS:
        log.warning("model output unusable")
        return "model", "", None

    sql = _clean_sql(text)
    # The slot is taken before run_query starts its clock, so waiting for a
    # turn does not count against the query time limit. The model has already
    # been called by this point and is not held up by the gate.
    if not GATE.acquire(timeout=WAIT_FOR_SLOT_S):
        log.warning("no query slot within %.1f seconds", WAIT_FOR_SLOT_S)
        return "busy", sql, None
    try:
        return None, sql, run_query(sql, db_path, timeout_s=timeout_s)
    except SandboxTimeout:
        return "timeout", sql, None
    except SandboxError as exc:
        log.warning("query not run: %s: %s", type(exc).__name__, exc)
        return "rejected", sql, None
    finally:
        GATE.release()


def ask(question, db_path, generate=generate_sql, timeout_s=QUERY_TIMEOUT_S, history=None, use_cooldown=True):
    """Answer a question. Never raises: failures come back in the Answer."""
    start = time.monotonic()
    kind, sql, result = None, "", None
    turns = []
    try:
        turns = clean_history(history)
    except ValueError:
        # Bad history is reported as bad input; its text is never logged.
        kind = "input"
    try:
        if kind is not None:
            pass
        elif _valid_question(question) and not _mentions_delimiter(question):
            kind, sql, result = _generate_and_run(
                question, db_path, generate, timeout_s, turns, use_cooldown
            )
        else:
            kind = "input"
    except Exception as exc:
        # Anything unexpected is treated as a refusal so no raw error escapes.
        log.warning("pipeline failed: %s: %s", type(exc).__name__, exc)
        kind = "rejected"

    elapsed = time.monotonic() - start
    shown = question if isinstance(question, str) else ""
    log.info(
        "question_len=%d question=%r sql=%r outcome=%s elapsed_s=%.3f",
        len(shown),
        shown[:200],
        sql[:500],
        kind or "ok",
        elapsed,
    )

    if kind is not None:
        return Answer(question, sql, [], [], False, elapsed, MESSAGES[kind], kind)
    columns, rows = result.columns, result.rows
    # Reached only after the sandbox ran the statement without error. Only the
    # exact sentinel statement swaps in the author-written text; no other
    # statement can, and the model never writes that text.
    if is_about_sql(sql):
        columns, rows = about_result()
    return Answer(
        question,
        sql,
        columns,
        rows,
        result.truncated,
        elapsed,
        None,
        None,
        result.elapsed_s,
    )
