import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

_SUFFIXES = ("", "-journal", "-wal", "-shm")
SnapshotState = dict[str, tuple[tuple[int, ...], str]]


@dataclass(frozen=True)
class SQLiteSnapshot:
    database: Path
    source_state: SnapshotState


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
        info.st_mtime_ns, info.st_ctime_ns,
    )


def _capture(database: Path, destination: Path | None) -> SnapshotState:
    result: SnapshotState = {}
    for suffix in _SUFFIXES:
        source = database.with_name(database.name + suffix)
        try:
            info = source.lstat()
        except FileNotFoundError:
            if not suffix:
                raise ValueError("SQLite snapshot database is missing") from None
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("SQLite snapshot requires regular files without symlinks")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb") as stream:
            before = _identity(os.fstat(stream.fileno()))
            if before != _identity(info):
                raise ValueError("SQLite snapshot source changed")
            digest = hashlib.sha256()
            if destination is None:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
            else:
                target = destination.with_name(destination.name + suffix)
                with target.open("xb") as output:
                    while block := stream.read(1024 * 1024):
                        digest.update(block)
                        output.write(block)
            if (
                _identity(os.fstat(stream.fileno())) != before
                or _identity(source.lstat()) != before
            ):
                raise ValueError("SQLite snapshot source changed")
        result[suffix] = (before, digest.hexdigest())
    return result


def verify_sqlite_source(database: Path, expected: SnapshotState) -> None:
    if _capture(database.absolute(), None) != expected:
        raise ValueError("SQLite snapshot source changed")


@contextmanager
def sqlite_snapshot(database: Path) -> Iterator[SQLiteSnapshot]:
    database = database.absolute()
    live_root = database.parent.resolve()
    with TemporaryDirectory(prefix="flight-agent-sqlite-snapshot-") as name:
        scratch = Path(name).resolve()
        if scratch == live_root or scratch.is_relative_to(live_root):
            raise ValueError("SQLite snapshot temporary directory overlaps the live database")
        copied = scratch / database.name
        initial = _capture(database, copied)
        copied_state = _capture(copied, None)
        if {
            suffix: (identity[4], digest)
            for suffix, (identity, digest) in copied_state.items()
        } != {
            suffix: (identity[4], digest)
            for suffix, (identity, digest) in initial.items()
        }:
            raise ValueError("SQLite snapshot copy verification failed")
        verify_sqlite_source(database, initial)
        yield SQLiteSnapshot(copied, initial)
        verify_sqlite_source(database, initial)
