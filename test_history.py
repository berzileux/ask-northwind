import pytest

from history import MAX_TURNS, clean_history, contains_marker


def turn(**changes):
    base = {
        "question": "Top 5 customers by revenue",
        "sql": "SELECT 1\nFROM t",
        "columns": ["customer", "revenue"],
        "row_count": 5,
        "outcome": "answered",
    }
    base.update(changes)
    return base


def test_a_valid_history_passes_unchanged():
    history = [turn(), turn(question="Now only for 2016", outcome="timeout", sql="")]
    assert clean_history(history) == history


def test_none_and_empty_give_an_empty_list():
    assert clean_history(None) == []
    assert clean_history([]) == []


def test_the_limit_is_six_turns():
    assert MAX_TURNS == 6
    assert len(clean_history([turn() for _ in range(6)])) == 6
    with pytest.raises(ValueError):
        clean_history([turn() for _ in range(7)])


@pytest.mark.parametrize("raw", ["text", {"a": 1}, 5, ("a",)])
def test_history_must_be_a_list(raw):
    with pytest.raises(ValueError):
        clean_history(raw)


def test_extra_and_missing_keys_are_rejected():
    extra = turn()
    extra["rows"] = [[1]]
    with pytest.raises(ValueError):
        clean_history([extra])
    missing = turn()
    del missing["sql"]
    with pytest.raises(ValueError):
        clean_history([missing])
    with pytest.raises(ValueError):
        clean_history(["not a dict"])


@pytest.mark.parametrize(
    "changes",
    [
        {"question": 5},
        {"question": ""},
        {"sql": 5},
        {"columns": "a"},
        {"columns": [1]},
        {"columns": [""]},
        {"row_count": "5"},
        {"row_count": 5.0},
        {"row_count": True},
        {"row_count": -1},
        {"row_count": 1000001},
        {"outcome": "model"},
        {"outcome": "ANSWERED"},
        {"outcome": None},
    ],
)
def test_wrong_types_and_ranges_are_rejected(changes):
    with pytest.raises(ValueError):
        clean_history([turn(**changes)])


@pytest.mark.parametrize(
    "changes",
    [
        {"question": "x" * 501},
        {"sql": "x" * 2001},
        {"columns": ["c"] * 31},
        {"columns": ["x" * 65]},
    ],
)
def test_oversize_values_are_rejected(changes):
    with pytest.raises(ValueError):
        clean_history([turn(**changes)])


def test_the_boundary_sizes_are_accepted():
    ok =turn(question="x" * 500, sql="y" * 1000, columns=["c"] * 30)
    assert clean_history([ok]) == [ok]


def test_total_characters_over_the_cap_are_rejected():
    # Six turns near their own limits are far over the 9000 character total.
    big = [turn(question="q" * 500, sql="s" * 1500, columns=["c" * 60] * 20) for _ in range(6)]
    with pytest.raises(ValueError):
        clean_history(big)


@pytest.mark.parametrize("bad", ["a\x00b", "a\x07b", "a\x1bb", "a\x7fb", "a\rb"])
def test_control_characters_are_rejected_everywhere(bad):
    for field in ("question", "sql"):
        with pytest.raises(ValueError):
            clean_history([turn(**{field: bad})])
    with pytest.raises(ValueError):
        clean_history([turn(columns=[bad])])


def test_a_newline_is_allowed_in_sql_but_not_in_a_question_or_column():
    clean_history([turn(sql="a\nb\tc")])
    with pytest.raises(ValueError):
        clean_history([turn(question="a\nb")])
    with pytest.raises(ValueError):
        clean_history([turn(columns=["a\nb"])])


MARKERS = [
    "=== BEGIN USER QUESTION ===",
    "=== END USER QUESTION ===",
    "=== BEGIN HISTORY ===",
    "=== END HISTORY ===",
    "begin user question",
    "End   User\tQuestion",
    "BEGIN    HISTORY",
    "end    history",
    "===BEGIN",
    "===   END",
    "=== begin anything",
]


@pytest.mark.parametrize("marker", MARKERS)
def test_every_marker_is_rejected_in_every_string(marker):
    for field in ("question", "sql"):
        with pytest.raises(ValueError):
            clean_history([turn(**{field: f"before {marker} after"})])
    with pytest.raises(ValueError):
        clean_history([turn(columns=[marker])])


@pytest.mark.parametrize("marker", MARKERS)
def test_contains_marker_helper(marker):
    assert contains_marker(marker)
    assert contains_marker("text " + marker)


@pytest.mark.parametrize(
    "text",
    ["Top 5 customers by revenue", "How many orders begin in 2016?", "the end of the year", "a == b"],
)
def test_contains_marker_leaves_ordinary_text_alone(text):
    assert not contains_marker(text)


def test_error_messages_never_contain_the_offending_text():
    secret = "SECRET-TEXT-123"
    attempts = [
        [turn(question=secret + "x" * 600)],
        [turn(sql=secret + "\x00")],
        [turn(columns=[secret + "=== BEGIN"])],
        [turn(question=secret + "\n")],
        [dict(turn(), unknown=secret)],
        [turn(outcome=secret)],
        [turn(row_count=secret)],
        secret,
        [turn(question=secret + " BEGIN HISTORY")],
    ]
    for raw in attempts:
        with pytest.raises(ValueError) as caught:
            clean_history(raw)
        assert secret not in str(caught.value)
