import json
import sys
import time
from pathlib import Path

from pipeline import ask
from sandbox import run_query

HERE = Path(__file__).parent

# Seconds to wait between model calls; tests set this to 0.
PAUSE_S = 2.0


def _normalise(rows):
    # Numbers are rounded so float noise does not fail a correct answer, and
    # rows are sorted so row order does not matter.
    def cell(value):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(round(value, 2))
        return value

    return sorted((tuple(cell(v) for v in row) for row in rows), key=repr)


def rows_match(reference_rows, actual_rows):
    return _normalise(reference_rows) == _normalise(actual_rows)


def compare_rows(reference_rows, actual_rows, ignore_columns=None, extra_columns_ok=False):
    """Compare like rows_match, after dropping the columns named by index.

    With extra_columns_ok only the first N columns of each model row count,
    where N is the width of the reference rows.
    """
    ignore = list(ignore_columns or [])
    for index in ignore:
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ValueError(f"ignore_columns must hold non-negative integers, got {index!r}")
    if ignore and reference_rows:
        width = len(reference_rows[0])
        for index in ignore:
            if index >= width:
                raise ValueError(f"ignore_columns index {index} is outside {width} columns")

    def keep(rows):
        return [tuple(v for i, v in enumerate(row) if i not in ignore) for row in rows]

    kept_reference = keep(reference_rows)
    kept_actual = keep(actual_rows)
    if extra_columns_ok and kept_reference:
        width = len(kept_reference[0])
        kept_actual = [row[:width] for row in kept_actual]
    return rows_match(kept_reference, kept_actual)


def parse_args(argv):
    """Return (database path or None, list of --only texts)."""
    path, only = None, []
    items = list(argv[1:])
    while items:
        item = items.pop(0)
        if item == "--only":
            if not items:
                raise SystemExit("--only needs a text")
            only.append(items.pop(0))
        elif path is None:
            path = item
        else:
            raise SystemExit(f"unexpected argument: {item}")
    return path, only


def main(argv):
    db_arg, only = parse_args(argv)
    db_path = Path(db_arg) if db_arg else HERE / "northwind.db"
    known = json.loads((HERE / "known_answers.json").read_text())
    if not known:
        print("known_answers.json is empty: nothing to evaluate")
        return 0
    if only:
        total = len(known)
        texts = [t.lower() for t in only]
        known = [i for i in known if any(t in i["question"].lower() for t in texts)]
        print(f"--only selected {len(known)} of {total} entries")
        if not known:
            print("no entry matches the --only values")
            return 0

    correct = 0
    total_elapsed = 0.0
    for number, item in enumerate(known):
        if number and PAUSE_S:
            time.sleep(PAUSE_S)
        # Only an entry that carries a history passes one, so older entries call ask as before.
        extra = {"history": item["history"]} if "history" in item else {}
        answer = ask(item["question"], db_path, **extra)
        reference = run_query(item["reference_sql"], db_path)
        # An entry may list other correct shapes of the same answer; any one matching is a pass.
        alternatives = []
        for number_alt, alt_sql in enumerate(item.get("alt_reference_sqls") or [], start=1):
            try:
                alternatives.append(run_query(alt_sql, db_path))
            except Exception as exc:
                raise RuntimeError(
                    f"alternative reference {number_alt} for {item['question']!r} could not be run: {type(exc).__name__}"
                ) from exc
        passed = answer.error is None and any(
            compare_rows(
                expected.rows,
                answer.rows,
                item.get("ignore_columns"),
                item.get("extra_columns_ok", False),
            )
            for expected in [reference] + alternatives
        )
        correct += passed
        total_elapsed += answer.elapsed_s
        print(f"{'pass' if passed else 'fail'}  {answer.elapsed_s:.2f}s  {item['question']}")
        if not passed:
            print(f"      outcome: {answer.error_kind or 'wrong answer'}")
            print(f"      model sql: {' '.join(answer.sql.split())[:400]}")
            print(f"      model rows: {answer.rows[:2]}")
            print(f"      expected rows: {reference.rows[:2]}")
            if alternatives:
                print(f"      alternatives tried: {len(alternatives)}")

    print(
        f"correct {correct} of {len(known)}, "
        f"average {total_elapsed / len(known):.2f}s per question"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
