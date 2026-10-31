import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from flight_agent.verification import sqlite_schema, sqlite_snapshot
from flight_agent.verification.sqlite_schema import DatabaseStartupError, migrate_database


@pytest.mark.parametrize("change", ["database", "new_sidecar", "removed_sidecar"])
def test_snapshot_rejects_source_changes_during_validation(tmp_path, change):
    database = tmp_path / "conversations.sqlite3"
    migrate_database(database)
    sidecar = database.with_name(database.name + "-journal")
    if change == "removed_sidecar":
        sidecar.write_bytes(b"pre-existing journal")
    before_scratch = set(Path.cwd().glob(".flight-agent-sqlite-snapshot-*"))

    with pytest.raises(ValueError, match="source changed"):
        with sqlite_snapshot.sqlite_snapshot(database):
            if change == "database":
                database.write_bytes(database.read_bytes() + b"change")
            elif change == "new_sidecar":
                sidecar.write_bytes(b"new journal")
            else:
                sidecar.unlink()

    assert set(Path.cwd().glob(".flight-agent-sqlite-snapshot-*")) == before_scratch


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_startup_rejects_symlinked_snapshot_sources_without_changes(tmp_path, suffix):
    database = tmp_path / "conversations.sqlite3"
    migrate_database(database)
    source = database.with_name(database.name + suffix)
    external = tmp_path / "external"
    if not suffix:
        database.rename(external)
    else:
        external.write_bytes(b"sidecar")
    source.symlink_to(external)
    before = {path.name: path.readlink() if path.is_symlink() else path.read_bytes()
              for path in tmp_path.iterdir()}

    with pytest.raises(DatabaseStartupError, match="regular files|symlink"):
        migrate_database(database)

    after = {path.name: path.readlink() if path.is_symlink() else path.read_bytes()
             for path in tmp_path.iterdir()}
    assert after == before


def test_snapshot_detects_damaged_copy_before_use(tmp_path, monkeypatch):
    database = tmp_path / "conversations.sqlite3"
    migrate_database(database)
    capture = sqlite_snapshot._capture

    def damage_copy(source, destination):
        state = capture(source, destination)
        if destination is not None:
            destination.write_bytes(b"damaged copy")
        return state

    monkeypatch.setattr(sqlite_snapshot, "_capture", damage_copy)
    with pytest.raises(DatabaseStartupError, match="copy verification"):
        migrate_database(database)


def test_startup_refuses_unstable_preflight_without_opening_live_database(tmp_path, monkeypatch):
    database = tmp_path / "conversations.sqlite3"
    migrate_database(database)
    before = database.read_bytes()
    validate = sqlite_schema._validate_database
    validations = []

    def invalidate_source(connection):
        version = validate(connection)
        validations.append(version)
        database.with_name(database.name + "-journal").write_bytes(b"new sidecar")
        return version

    monkeypatch.setattr(sqlite_schema, "_validate_database", invalidate_source)
    with pytest.raises(DatabaseStartupError, match="source changed"):
        migrate_database(database)

    assert validations == [sqlite_schema.MIGRATIONS[-1].version]
    assert database.read_bytes() == before


@pytest.mark.parametrize("cwd_location", ["unwritable", "database_root"])
def test_snapshot_scratch_is_independent_of_current_working_directory(
    tmp_path, monkeypatch, cwd_location
):
    root = tmp_path / "state"
    root.mkdir()
    database = root / "conversations.sqlite3"
    migrate_database(database)
    cwd = root if cwd_location == "database_root" else tmp_path / "unwritable"
    if cwd_location == "unwritable":
        cwd.mkdir()
        cwd.chmod(0o500)
    monkeypatch.chdir(cwd)
    try:
        migrate_database(database)
    finally:
        if cwd_location == "unwritable":
            cwd.chmod(0o700)


def _hot_database(path: Path, *, damage: str) -> None:
    migrate_database(path)
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    connection.execute("CREATE TABLE pressure (id INTEGER PRIMARY KEY, payload BLOB)")
    connection.executemany(
        "INSERT INTO pressure (payload) VALUES (?)", [(b"a" * 2000,)] * 300
    )
    if damage == "future":
        connection.execute("PRAGMA user_version=999")
    connection.commit()
    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    root_page = connection.execute(
        "SELECT rootpage FROM sqlite_master WHERE name='task_events'"
    ).fetchone()[0]
    connection.close()
    script = """
import os
import sqlite3
connection = sqlite3.connect(os.environ["DATABASE"], isolation_level=None)
connection.execute("PRAGMA cache_size=8")
connection.execute("PRAGMA cache_spill=ON")
connection.execute("BEGIN IMMEDIATE")
connection.execute("UPDATE pressure SET payload=?", (b"b" * 2000,))
os._exit(73)
"""
    process = subprocess.run(
        [sys.executable, "-c", script],
        env=dict(os.environ, DATABASE=str(path)),
        timeout=15,
        capture_output=True,
    )
    assert process.returncode == 73, process.stderr
    journal = path.with_name(path.name + "-journal")
    assert journal.read_bytes()[:8] == bytes.fromhex("d9d505f920a163d7")
    if damage == "corrupt":
        data = bytearray(path.read_bytes())
        data[(root_page - 1) * page_size] = 0
        path.write_bytes(data)


@pytest.mark.parametrize("damage", ["future", "corrupt"])
def test_startup_refuses_raced_database_before_recovery_or_writes(
    tmp_path, monkeypatch, damage
):
    live = tmp_path / "live" / "conversations.sqlite3"
    live.parent.mkdir()
    migrate_database(live)
    replacement = tmp_path / "replacement" / "conversations.sqlite3"
    replacement.parent.mkdir()
    _hot_database(replacement, damage=damage)
    replacement_journal = replacement.with_name(replacement.name + "-journal")
    expected_main = replacement.read_bytes()
    expected_journal = replacement_journal.read_bytes()
    expected_inventory = {
        path.name: path.read_bytes() for path in live.parent.iterdir()
    }
    expected_inventory[live.name] = expected_main
    expected_inventory[live.name + "-journal"] = expected_journal
    connect = sqlite_schema.connect_database
    calls = 0

    def race(path, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            os.replace(replacement, live)
            os.replace(
                replacement_journal,
                live.with_name(live.name + "-journal"),
            )
        return connect(path, **kwargs)

    monkeypatch.setattr(sqlite_schema, "connect_database", race)
    with pytest.raises(DatabaseStartupError, match="changed|newer|integrity|corrupt"):
        migrate_database(live)

    assert {
        path.name: path.read_bytes() for path in live.parent.iterdir()
    } == expected_inventory
