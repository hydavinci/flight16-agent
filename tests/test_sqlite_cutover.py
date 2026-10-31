import pytest

from flight_agent.verification import sqlite_cutover as cutover
from flight_agent.verification.sqlite_schema import DatabaseStartupError


def test_nonempty_cutover_backs_up_bytes_without_parsing_or_deleting_json(tmp_path):
    root = tmp_path / "verification"
    record = root / ("a" * 32) / "conversation.json"
    record.parent.mkdir(parents=True)
    record.write_bytes(b'{"unreadable-history":')
    evidence = record.parent / "tasks" / "kept" / "metric.bin"
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b"\x00disk evidence\xff")
    backup = cutover.prepare_cutover(root, require_empty=False)
    assert backup is not None
    assert (backup / record.relative_to(root)).read_bytes() == record.read_bytes()
    assert (backup / evidence.relative_to(root)).read_bytes() == evidence.read_bytes()
    assert not (root / "conversations.sqlite3").exists()
    assert not list(tmp_path.glob("*.partial"))


def test_explicit_empty_mode_rejects_nonempty_state_without_modification(tmp_path):
    root = tmp_path / "verification"
    root.mkdir()
    (root / "keep").write_bytes(b"untouched")
    with pytest.raises(DatabaseStartupError, match="empty"):
        cutover.prepare_cutover(root, require_empty=True)
    assert (root / "keep").read_bytes() == b"untouched"
    assert sorted(path.name for path in root.iterdir()) == ["keep"]
    empty = tmp_path / "empty"
    assert cutover.prepare_cutover(empty, require_empty=True) is None
    assert empty.is_dir()


def test_backup_failure_prevents_database_creation(tmp_path, monkeypatch):
    root = tmp_path / "verification"
    root.mkdir()
    (root / "keep").write_text("original")

    def fail(source, target, **kwargs):
        raise OSError("backup device unavailable")

    monkeypatch.setattr(cutover.shutil, "copytree", fail)
    with pytest.raises(DatabaseStartupError, match="backup"):
        cutover.prepare_cutover(root, require_empty=False)
    assert (root / "keep").read_text() == "original"
    assert not (root / "conversations.sqlite3").exists()


def test_backup_refuses_symlinks_without_reading_external_content(tmp_path):
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "source.m").write_text("user source")
    root = tmp_path / "verification"
    root.mkdir()
    (root / "linked-project").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DatabaseStartupError, match="symlink"):
        cutover.prepare_cutover(root, require_empty=False)
    assert (outside / "source.m").read_text() == "user source"
    assert not (root / "conversations.sqlite3").exists()
