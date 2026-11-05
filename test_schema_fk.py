import sqlite3

from schema import schema_data, schema_text


def make_database(path, statements):
    conn = sqlite3.connect(path)
    conn.executescript(statements)
    conn.close()


def fk_lines(text):
    return [line for line in text.splitlines() if line.startswith("FK ")]


def test_one_foreign_key_gives_exactly_one_fk_line(tmp_path):
    path = tmp_path / "two.db"
    make_database(
        path,
        """
        CREATE TABLE Parent (ParentID INTEGER PRIMARY KEY, Label TEXT);
        CREATE TABLE Child (
            ChildID INTEGER PRIMARY KEY,
            ParentID INTEGER REFERENCES Parent (ParentID)
        );
        """,
    )
    assert fk_lines(schema_text(path)) == ["FK Child.ParentID -> Parent.ParentID"]


def test_table_without_foreign_keys_gets_no_fk_line(tmp_path):
    path = tmp_path / "plain.db"
    make_database(path, "CREATE TABLE Solo (SoloID INTEGER PRIMARY KEY, Label TEXT);")
    assert fk_lines(schema_text(path)) == []


def test_fk_line_sits_after_the_column_lines_of_its_table(tmp_path):
    path = tmp_path / "order.db"
    make_database(
        path,
        """
        CREATE TABLE Parent (ParentID INTEGER PRIMARY KEY);
        CREATE TABLE Child (ChildID INTEGER, ParentID INTEGER REFERENCES Parent (ParentID));
        """,
    )
    block = schema_text(path).split("\n\n")[0]
    assert block.splitlines() == [
        "Table Child",
        "  ChildID INTEGER",
        "  ParentID INTEGER",
        "FK Child.ParentID -> Parent.ParentID",
    ]


def test_foreign_key_without_a_named_target_uses_the_primary_key(tmp_path):
    path = tmp_path / "implicit.db"
    make_database(
        path,
        """
        CREATE TABLE Parent (ParentID INTEGER PRIMARY KEY);
        CREATE TABLE Child (ChildID INTEGER, Ref INTEGER REFERENCES Parent);
        """,
    )
    assert fk_lines(schema_text(path)) == ["FK Child.Ref -> Parent.ParentID"]


def test_names_with_spaces_are_quoted(tmp_path):
    path = tmp_path / "quoted.db"
    make_database(
        path,
        """
        CREATE TABLE "Big Parent" (ParentID INTEGER PRIMARY KEY);
        CREATE TABLE Child (ParentID INTEGER REFERENCES "Big Parent" (ParentID));
        """,
    )
    assert fk_lines(schema_text(path)) == ['FK Child.ParentID -> "Big Parent".ParentID']


def test_output_is_the_same_across_two_calls(tmp_path):
    path = tmp_path / "twice.db"
    make_database(
        path,
        """
        CREATE TABLE A (AID INTEGER PRIMARY KEY);
        CREATE TABLE B (BID INTEGER PRIMARY KEY);
        CREATE TABLE C (
            X INTEGER REFERENCES B (BID),
            Y INTEGER REFERENCES A (AID)
        );
        """,
    )
    assert schema_text(path) == schema_text(path)
    assert fk_lines(schema_text(path)) == ["FK C.X -> B.BID", "FK C.Y -> A.AID"]


def test_schema_data_matches_the_text(tmp_path):
    path = tmp_path / "data.db"
    make_database(
        path,
        """
        CREATE TABLE Parent (ParentID INTEGER PRIMARY KEY);
        CREATE TABLE Child (ParentID INTEGER REFERENCES Parent (ParentID));
        """,
    )
    assert schema_data(path) == [
        {
            "name": "Child",
            "columns": [{"name": "ParentID", "type": "INTEGER"}],
            "foreign_keys": [
                {"column": "ParentID", "ref_table": "Parent", "ref_column": "ParentID"}
            ],
        },
        {
            "name": "Parent",
            "columns": [{"name": "ParentID", "type": "INTEGER"}],
            "foreign_keys": [],
        },
    ]


def test_real_database_has_thirteen_fk_lines(real_db_path):
    assert len(fk_lines(schema_text(real_db_path))) == 13
