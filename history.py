import re
import unicodedata

# Six earlier questions are enough to refer back past the last few, and few enough to keep the prompt short.
MAX_TURNS = 6
# One question is at most this long, the same limit the pipeline puts on a question.
MAX_QUESTION_CHARS = 500
MAX_SQL_CHARS = 2000
MAX_COLUMNS = 30
MAX_COLUMN_CHARS = 64
MAX_ROW_COUNT = 1000000
# About 1500 characters per turn over six turns. Together with a question (500 characters) and the
# JSON around them it must stay under the request body cap of 16384 bytes; see the check below.
MAX_TOTAL_CHARS = 9000
# Must equal the body cap in app.py (a test compares them).
REQUEST_BODY_CAP = 16384
# Room for the JSON keys, quotes and commas of six turns and the request around them.
BODY_OVERHEAD_MARGIN = 2000

# A larger turn count or total cap that no longer fits in the request body fails at import.
# This counts one byte per character; text with many accents, symbols or escaped quotes takes more
# bytes, and the server then answers 413 instead of accepting a body over the cap.
assert MAX_TOTAL_CHARS + MAX_QUESTION_CHARS + BODY_OVERHEAD_MARGIN < REQUEST_BODY_CAP, "history does not fit the body cap"
OUTCOMES = ("answered", "rejected", "timeout")
KEYS = ("question", "sql", "columns", "row_count", "outcome")

# Compared after upper-casing and collapsing runs of whitespace, so spacing and
# case variants of a marker are caught as well.
_MARKER_WORDS = (
    "BEGIN USER QUESTION",
    "END USER QUESTION",
    "BEGIN HISTORY",
    "END HISTORY",
)
_FENCE = re.compile(r"===\s*(BEGIN|END)")


def contains_marker(text):
    """True when text holds any prompt marker, in any case or spacing."""
    folded = " ".join(str(text).upper().split())
    if any(word in folded for word in _MARKER_WORDS):
        return True
    return bool(_FENCE.search(folded))


def _bad(text, newline_ok):
    for ch in text:
        if ch == "\t" or (ch == "\n" and newline_ok):
            continue
        if unicodedata.category(ch) == "Cc":
            return True
    return False


def _text(value, low, high, newline_ok, where):
    if not isinstance(value, str) or not low <= len(value) <= high:
        raise ValueError(f"{where} has the wrong type or length")
    if _bad(value, newline_ok):
        raise ValueError(f"{where} has a control character")
    if contains_marker(value):
        raise ValueError(f"{where} has a reserved marker")
    return value


def _turn(item, number):
    where = f"history turn {number}"
    if not isinstance(item, dict) or set(item) != set(KEYS):
        raise ValueError(f"{where} must have exactly the keys {', '.join(KEYS)}")
    question = _text(item["question"], 1, MAX_QUESTION_CHARS, False, f"{where} question")
    sql = _text(item["sql"], 0, MAX_SQL_CHARS, True, f"{where} sql")
    columns = item["columns"]
    if not isinstance(columns, list) or len(columns) > MAX_COLUMNS:
        raise ValueError(f"{where} columns must be a short list")
    columns = [_text(c, 1, MAX_COLUMN_CHARS, False, f"{where} column") for c in columns]
    count = item["row_count"]
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= MAX_ROW_COUNT:
        raise ValueError(f"{where} row_count is out of range")
    if item["outcome"] not in OUTCOMES:
        raise ValueError(f"{where} outcome is not allowed")
    return {
        "question": question,
        "sql": sql,
        "columns": columns,
        "row_count": count,
        "outcome": item["outcome"],
    }


def clean_history(raw):
    """Return a list of valid turns; raise ValueError with a short message."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("history must be a list")
    if len(raw) > MAX_TURNS:
        raise ValueError(f"history may hold at most {MAX_TURNS} turns")
    turns = [_turn(item, number) for number, item in enumerate(raw, start=1)]
    total = sum(
        len(t["question"]) + len(t["sql"]) + sum(len(c) for c in t["columns"]) for t in turns
    )
    if total > MAX_TOTAL_CHARS:
        raise ValueError("history is too long")
    return turns
