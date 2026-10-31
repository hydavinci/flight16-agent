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

    assert validations == [1]
    assert database.read_bytes() == before
