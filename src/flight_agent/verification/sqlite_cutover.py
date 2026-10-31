import hashlib
import os
import shutil
from pathlib import Path
from uuid import uuid4

from .intake import safe_directory
from .sqlite_schema import DatabaseStartupError


def _inventory(root: Path) -> dict[str, tuple[int, str] | None]:
    result: dict[str, tuple[int, str] | None] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise DatabaseStartupError(f"Cutover refuses symlink: {path}")
        name = path.relative_to(root).as_posix()
        if path.is_dir():
            result[name] = None
        elif path.is_file():
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    size += len(block)
                    digest.update(block)
            result[name] = (size, digest.hexdigest())
        else:
            raise DatabaseStartupError(f"Cutover refuses nonregular entry: {path}")
    return result


def prepare_cutover(root: Path, *, require_empty: bool) -> Path | None:
    safe_directory(root)
    root.mkdir(parents=True, exist_ok=True)
    database = root / "conversations.sqlite3"
    if database.is_symlink():
        raise DatabaseStartupError("Cutover refuses database symlink")
    if database.exists():
        if require_empty:
            raise DatabaseStartupError("Development state must be explicitly empty")
        return None
    if require_empty:
        if any(root.iterdir()):
            raise DatabaseStartupError("Development state must be explicitly empty")
        return None
    target = root.with_name(f"{root.name}.pre-sqlite-{uuid4().hex}")
    partial = target.with_name(target.name + ".partial")
    try:
        before = _inventory(root)
        shutil.copytree(root, partial, symlinks=True)
        if _inventory(root) != before or _inventory(partial) != before:
            raise DatabaseStartupError("Cutover backup verification failed")
        for path in partial.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        partial.rename(target)
        return target
    except (OSError, ValueError) as exc:
        raise DatabaseStartupError(f"Cutover backup failed: {exc}") from exc
    finally:
        if partial.exists():
            shutil.rmtree(partial)
