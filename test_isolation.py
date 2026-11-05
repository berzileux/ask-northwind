import sqlite3

import pytest

import sandbox
from conftest import file_hash
from sandbox import SandboxSetupError, run_query


def test_connections_are_distinct(db_path):
    first = sandbox._open_sandbox(db_path)
    second = sandbox._open_sandbox(db_path)
    try:
        assert first is not second
    finally:
        first.close()
        second.close()


def test_damage_to_one_connection_does_not_reach_another_or_the_source(db_path):
    before_hash = file_hash(db_path)
    first = sandbox._open_sandbox(db_path)
    original_count = first.execute("SELECT count(*) FROM Customers").fetchone()[0]

    # Test code removes every guard on the first connection only.
    first.set_authorizer(None)
    first.execute("PRAGMA query_only=OFF")
    first.execute("DELETE FROM Customers")
    first.commit()
    assert first.execute("SELECT count(*) FROM Customers").fetchone()[0] == 0

    second = sandbox._open_sandbox(db_path)
    try:
        assert (
            second.execute("SELECT count(*) FROM Customers").fetchone()[0]
            == original_count
        )
    finally:
        first.close()
        second.close()

    assert file_hash(db_path) == before_hash


def test_setup_failure_fails_closed(db_path, monkeypatch):
    real_connect = sqlite3.connect

    def refuse(*args, **kwargs):
        raise sqlite3.OperationalError("authorizer unavailable")

    # Subclassing the connection type is the only way to override a C method.
    failing = type("Failing", (sqlite3.Connection,), {"set_authorizer": refuse})

    def connect(database, *args, **kwargs):
        if database == ":memory:":
            return real_connect(database, *args, factory=failing, **kwargs)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sandbox.sqlite3, "connect", connect)

    with pytest.raises(SandboxSetupError):
        run_query("SELECT 1", db_path)
