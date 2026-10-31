import hashlib
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

_SUFFIXES = ("", "-journal", "-wal", "-shm")


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
        info.st_mtime_ns, info.st_ctime_ns,
    )


def _capture(database: Path, destination: Path | None) -> dict[str, tuple[tuple[int, ...], str]]:
    result: dict[str, tuple[tuple[int, ...], str]] = {}
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


@contextmanager
def sqlite_snapshot(database: Path) -> Iterator[Path]:
    database = database.absolute()
    workspace = Path.cwd().resolve()
    if workspace.is_relative_to(database.parent.resolve()):
        raise ValueError("SQLite snapshot workspace overlaps the live database directory")
    scratch = workspace / f".flight-agent-sqlite-snapshot-{uuid4().hex}"
    scratch.mkdir(mode=0o700)
    try:
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
        if _capture(database, None) != initial:
            raise ValueError("SQLite snapshot source changed")
        yield copied
        if _capture(database, None) != initial:
            raise ValueError("SQLite snapshot source changed")
    finally:
        shutil.rmtree(scratch)
