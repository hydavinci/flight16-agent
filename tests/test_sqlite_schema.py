import sqlite3

import pytest

from flight_agent.verification import sqlite_schema as schema


def test_schema_is_versioned_idempotent_and_wal(tmp_path):
    path = tmp_path / "conversations.sqlite3"
    schema.migrate_database(path)
    with schema.connect_database(path) as connection:
        before = list(connection.execute("SELECT * FROM schema_migrations"))
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "conversations",
            "messages",
            "tasks",
            "task_events",
            "calls",
            "artifacts",
            "schema_migrations",
        } <= names
    schema.migrate_database(path)
    with schema.connect_database(path) as connection:
        assert list(connection.execute("SELECT * FROM schema_migrations")) == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_future_schema_is_refused_without_rewriting_it(tmp_path):
    path = tmp_path / "conversations.sqlite3"
    schema.migrate_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(schema.DatabaseStartupError, match="newer"):
        schema.migrate_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999
        assert (
            connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1
        )


def test_failed_upgrade_rolls_back_ddl_and_version(tmp_path, monkeypatch):
    path = tmp_path / "conversations.sqlite3"
    schema.migrate_database(path)
    broken = schema.Migration(
        2,
        "failure-probe",
        (
            "CREATE TABLE migration_probe (value TEXT)",
            "INSERT INTO missing_migration_table VALUES (1)",
        ),
    )
    monkeypatch.setattr(schema, "MIGRATIONS", (*schema.MIGRATIONS, broken))
    with pytest.raises(schema.DatabaseStartupError, match="migration"):
        schema.migrate_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='migration_probe'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1
        )


def test_migration_ledger_drift_is_refused(tmp_path):
    path = tmp_path / "conversations.sqlite3"
    schema.migrate_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE schema_migrations SET sha256=?", ("0" * 64,))
    with pytest.raises(schema.DatabaseStartupError, match="ledger"):
        schema.migrate_database(path)


def test_next_migration_preserves_existing_data(tmp_path, monkeypatch):
    path = tmp_path / "conversations.sqlite3"
    schema.migrate_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE retained_probe (value TEXT)")
        connection.execute("INSERT INTO retained_probe VALUES ('keep')")
    upgrade = schema.Migration(2, "upgrade-probe", ("CREATE TABLE upgraded_probe (value TEXT)",))
    monkeypatch.setattr(schema, "MIGRATIONS", (*schema.MIGRATIONS, upgrade))
    schema.migrate_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM retained_probe").fetchone()[0] == "keep"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 2
        )


def test_future_ledger_alone_is_refused(tmp_path):
    path = tmp_path / "conversations.sqlite3"
    schema.migrate_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations VALUES (999, 'future', ?, '2026-10-31')",
            ("f" * 64,),
        )
    with pytest.raises(schema.DatabaseStartupError, match="newer"):
        schema.migrate_database(path)
