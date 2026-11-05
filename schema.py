import re
import sqlite3
from pathlib import Path

_PLAIN_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+")


def _sql_name(name):
    # Anything outside letters, digits and underscore must be quoted exactly
    # as it has to be written in SQL, or the model's query will not parse.
    if _PLAIN_IDENTIFIER.fullmatch(name):
        return name
    return '"' + name.replace('"', '""') + '"'


def _connect_read_only(db_path):
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _target_column(conn, ref_table, ref_column, position):
    # A foreign key without a named target points at the primary key of the
    # other table; position picks the matching part of a composite key.
    if ref_column is not None:
        return ref_column
    keys = sorted(
        (pk, name)
        for _cid, name, _type, _notnull, _default, pk in conn.execute(
            f"PRAGMA table_info({_sql_name_for_pragma(ref_table)})"
        )
        if pk
    )
    return keys[position][1] if position < len(keys) else ref_table


def schema_data(db_path):
    """Return every table with its columns and foreign keys, read from the file."""
    conn = _connect_read_only(db_path)
    try:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables = []
        for table in names:
            columns = [
                {"name": column, "type": col_type}
                for _cid, column, col_type, *_rest in conn.execute(
                    f"PRAGMA table_info({_sql_name_for_pragma(table)})"
                )
            ]
            keys = sorted(
                (
                    from_column,
                    ref_table,
                    _target_column(conn, ref_table, ref_column, seq),
                )
                for _id, seq, ref_table, from_column, ref_column, *_rest in conn.execute(
                    f"PRAGMA foreign_key_list({_sql_name_for_pragma(table)})"
                )
            )
            tables.append(
                {
                    "name": table,
                    "columns": columns,
                    "foreign_keys": [
                        {"column": c, "ref_table": t, "ref_column": rc} for c, t, rc in keys
                    ],
                }
            )
        return tables
    finally:
        conn.close()


def schema_text(db_path):
    """Describe every table, its columns and its foreign keys, read on each call."""
    blocks = []
    for table in schema_data(db_path):
        lines = [f"Table {_sql_name(table['name'])}"]
        for column in table["columns"]:
            lines.append(f"  {_sql_name(column['name'])} {column['type']}".rstrip())
        for key in table["foreign_keys"]:
            lines.append(
                f"FK {_sql_name(table['name'])}.{_sql_name(key['column'])} -> "
                f"{_sql_name(key['ref_table'])}.{_sql_name(key['ref_column'])}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _sql_name_for_pragma(name):
    # PRAGMA arguments are always quoted so odd table names are read correctly.
    return '"' + name.replace('"', '""') + '"'
