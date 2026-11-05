import re
from pathlib import Path

HERE = Path(__file__).parent
MAX_ABOUT_BYTES = 1500

# The model only ever returns this statement for an about question; the text
# shown to the visitor comes from the file, never from the model.
ABOUT_SQL = "SELECT 'about_database' AS about"


def load_about(directory=HERE):
    # A missing or unusable file stops the import, so a broken deploy fails at
    # start instead of showing an empty answer.
    try:
        raw = (Path(directory) / "about.md").read_bytes()
    except OSError as exc:
        raise RuntimeError("about.md could not be read") from exc
    if not raw.strip():
        raise RuntimeError("about.md is empty")
    if len(raw) > MAX_ABOUT_BYTES:
        raise RuntimeError("about.md is too long")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeError("about.md must be ASCII") from exc
    return text.strip()


ABOUT_TEXT = load_about()


def _fold(sql):
    text = sql.strip()
    if text.endswith(";"):
        text = text[:-1]
    return re.sub(r"\s+", " ", text).strip().upper()


def is_about_sql(sql):
    """True only for the sentinel statement, ignoring case, spacing and one final semicolon."""
    if not isinstance(sql, str):
        return False
    return _fold(sql) == _fold(ABOUT_SQL)


def about_result():
    return ["about"], [[ABOUT_TEXT]]
