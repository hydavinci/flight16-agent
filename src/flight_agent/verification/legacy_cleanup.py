import json
import os
import shutil
import sqlite3
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..models import JOB_ID
from ..runtime.resources import ResourceReference
from .intake import safe_directory
from .sqlite_codec import decode
from .sqlite_schema import MIGRATIONS
from .sqlite_snapshot import sqlite_snapshot

RecordKind = Literal["v1", "v2", "unknown"]


@dataclass
class CleanupReport:
    v1: list[str] = field(default_factory=list)
    v2: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


def classify_record(data: object) -> RecordKind:
    if not isinstance(data, dict):
        return "unknown"
    if "schema_version" in data and data["schema_version"] not in ("1.0", "2.0"):
        return "unknown"
    archived = data.get("archived_tasks", [])
    if not isinstance(archived, list):
        return "unknown"
    tasks = [*([data["task"]] if data.get("task") is not None else []), *archived]
    if any(not isinstance(item, dict) for item in tasks):
        return "unknown"
    if all(
        item.get("schema_version") == "2.0" and "scenario" not in item for item in tasks
    ) and (tasks or data.get("schema_version") == "2.0"):
        if data.get("schema_version") == "1.0":
            return "unknown"
        return "v2"
    if tasks and data.get("schema_version") != "2.0" and all(
        item.get("scenario") in ("d1", "d2", "general")
        and ("schema_version" not in item or item["schema_version"] == "1.0")
        for item in tasks
    ):
        return "v1"
    return "unknown"


def normalize_v2_record(data: dict[str, object]) -> dict[str, object]:
    normalized = dict(data)
    for name in ("scenario", "goal", "active_workflow", "workflow_agenda"):
        normalized.pop(name, None)
    normalized["schema_version"] = "2.0"
    return normalized


def _path_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _owned_path_components(locator: Path) -> tuple[set[Path], Path]:
    if not locator.is_absolute() or ".." in locator.parts:
        raise ValueError("Recorded path ownership is unresolved")
    current = Path(locator.anchor)
    remaining = list(locator.parts[1:])
    protected: set[Path] = set()
    observed: dict[Path, tuple[tuple[int, ...], str | None]] = {}
    symlinks: set[tuple[int, int]] = set()
    steps = 0
    while remaining:
        steps += 1
        if steps > 4096:
            raise ValueError("Recorded path ownership traversal is ambiguous")
        component = remaining.pop(0)
        if component in ("", "."):
            continue
        if component == "..":
            if current == Path(current.anchor):
                raise ValueError("Recorded path ownership traversal escapes the filesystem root")
            current = current.parent
            protected.add(current)
            continue
        entry = current / component
        info = entry.lstat()
        identity = _path_identity(info)
        target = os.readlink(entry) if stat.S_ISLNK(info.st_mode) else None
        previous = observed.setdefault(entry, (identity, target))
        if previous != (identity, target):
            raise ValueError("Recorded path ownership traversal is ambiguous")
        protected.add(entry)
        if stat.S_ISLNK(info.st_mode):
            key = (info.st_dev, info.st_ino)
            if key in symlinks:
                raise ValueError("Recorded path ownership contains a symlink cycle")
            symlinks.add(key)
            link = Path(target or "")
            if not target:
                raise ValueError("Recorded path ownership contains an empty symlink")
            if link.is_absolute():
                current = Path(link.anchor)
                remaining = [*link.parts[1:], *remaining]
            else:
                remaining = [*link.parts, *remaining]
            continue
        if remaining and not stat.S_ISDIR(info.st_mode):
            raise ValueError("Recorded path ownership traverses a non-directory")
        if not remaining and not (
            stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
        ):
            raise ValueError("Recorded path ownership endpoint is nonregular")
        current = entry
    for entry, expected in observed.items():
        info = entry.lstat()
        target = os.readlink(entry) if stat.S_ISLNK(info.st_mode) else None
        if (_path_identity(info), target) != expected:
            raise ValueError("Recorded path ownership traversal is ambiguous")
    return protected, current


def _record_paths(data: Any, *, legacy_directory: Path | None) -> list[Path]:
    if not isinstance(data, dict):
        raise ValueError("Record ownership is unreadable")
    if "schema_version" in data and data["schema_version"] not in ("1.0", "2.0"):
        raise ValueError("Record ownership schema is unknown")
    sources = [data.get("project_root")]
    outputs = [data.get("output_root")]
    archived = data.get("archived_tasks", [])
    if not isinstance(archived, list):
        raise ValueError("Task ownership container is malformed")
    tasks = [*([data["task"]] if data.get("task") is not None else []), *archived]
    for task in tasks:
        if not isinstance(task, dict) or classify_record({"task": task}) == "unknown":
            raise ValueError("Task ownership entry is malformed")
        if (
            any(task.get(name) for name in ("artifacts", "checkpoints", "outputs", "analysis_runs"))
            and not task.get("result_directory")
        ):
            raise ValueError("Task evidence ownership is unresolved")
        if "project" in task:
            if not isinstance(task["project"], dict) or not task["project"].get("root"):
                raise ValueError("Project ownership is malformed")
            sources.append(task["project"]["root"])
        outputs.extend((task.get("result_directory"), task.get("output_root")))
        if "baseline" in task:
            baseline = task["baseline"]
            if (
                not isinstance(baseline, dict)
                or not isinstance(baseline.get("resources", []), list)
            ):
                raise ValueError("Resource ownership is malformed")
            for raw in baseline.get("resources", []):
                resource = ResourceReference.model_validate(raw)
                if resource.location not in ("agent", "worker"):
                    raise ValueError("Resource location ownership is unknown")
                sources.append(resource.locator)
    paths: list[Path] = []
    for source in [*sources, *outputs]:
        if source is None:
            continue
        if not isinstance(source, str) or not source or not Path(source).is_absolute():
            raise ValueError("Recorded path ownership is unresolved")
        locator = Path(source)
        if ".." in locator.parts:
            raise ValueError("Recorded path ownership contains traversal")
        components, path = _owned_path_components(locator)
        if (
            source in sources
            or legacy_directory is None
            or not locator.is_relative_to(legacy_directory)
            or not path.is_relative_to(legacy_directory)
        ):
            paths.extend(components)
    return paths


def _sqlite_paths(root: Path) -> set[Path]:
    database = root / "conversations.sqlite3"
    for path in (
        database,
        *(root / f"conversations.sqlite3-{suffix}" for suffix in ("wal", "shm", "journal")),
    ):
        if path.is_symlink():
            raise ValueError("SQLite ownership contains a symlink")
    if not database.exists():
        if any(
            (root / f"conversations.sqlite3-{suffix}").exists()
            for suffix in ("wal", "shm", "journal")
        ):
            raise ValueError("SQLite ownership database is missing")
        return set()
    if not database.is_file():
        raise ValueError("SQLite ownership database is not a file")
    with sqlite_snapshot(database) as snapshot:
        return _sqlite_snapshot_paths(root, snapshot.database)


def _sqlite_snapshot_paths(root: Path, snapshot: Path) -> set[Path]:
    connection = sqlite3.connect(snapshot, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ValueError("SQLite ownership integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("SQLite ownership has invalid references")
        if connection.execute("PRAGMA user_version").fetchone()[0] != MIGRATIONS[-1].version:
            raise ValueError("SQLite ownership schema is unsupported")
        ledger = connection.execute(
            "SELECT version, name, sha256 FROM schema_migrations ORDER BY version"
        ).fetchall()
        if [tuple(row) for row in ledger] != [
            (migration.version, migration.name, migration.sha256) for migration in MIGRATIONS
        ]:
            raise ValueError("SQLite ownership migration ledger is invalid")
        protected: set[Path] = set()
        for row in connection.execute("SELECT id FROM conversations ORDER BY id"):
            record = decode(connection, row["id"])
            directory = root / record.id
            safe_directory(directory)
            if not directory.is_dir():
                raise ValueError("SQLite conversation directory is missing")
            protected.add(directory.resolve())
            if record.project_root is not None:
                project = Path(record.project_root)
                if not project.is_absolute():
                    raise ValueError("SQLite project ownership is unresolved")
                if ".." in project.parts:
                    raise ValueError("SQLite project ownership contains traversal")
                components, _ = _owned_path_components(project)
                protected.update(components)
            if record.output_root is not None:
                path = Path(record.output_root)
                safe_directory(path)
                protected.add(path.resolve())
            for task in [*record.archived_tasks, *([record.task] if record.task else [])]:
                for resource in task.baseline.resources:
                    path = Path(resource.locator)
                    if not path.is_absolute():
                        raise ValueError("SQLite resource ownership is unresolved")
                    if ".." in path.parts:
                        raise ValueError("SQLite resource ownership contains traversal")
                    components, _ = _owned_path_components(path)
                    protected.update(components)
                expected = (
                    Path(task.output_root) / f"flight-agent-{task.id}"
                    if task.output_root
                    else root / record.id / "tasks" / task.id
                )
                safe_directory(expected)
                if str(expected) != task.result_directory:
                    raise ValueError("SQLite task directory identity mismatch")
                has_artifacts = connection.execute(
                    "SELECT 1 FROM artifacts WHERE task_id=? LIMIT 1", (task.id,)
                ).fetchone()
                if (
                    task.baseline.confirmed
                    or task.calls
                    or task.artifacts
                    or task.analysis_runs
                    or task.checkpoints
                    or task.checkpoint_sha256
                    or task.outputs
                    or task.result is not None
                    or has_artifacts is not None
                ) and not expected.is_dir():
                    raise ValueError("SQLite task directory is missing")
                protected.add(expected.resolve())
                if task.output_root is not None:
                    output = Path(task.output_root)
                    safe_directory(output)
                    protected.add(output.resolve())
        return protected
    finally:
        connection.close()


def cleanup_legacy_records(root: Path, *, apply: bool) -> CleanupReport:
    safe_directory(root.absolute())
    root = root.resolve(strict=True)
    report = CleanupReport()
    candidates: list[Path] = []
    protected: set[Path] = set()
    unresolved = False
    try:
        protected.update(_sqlite_paths(root))
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, TypeError):
        report.unknown.append("conversations.sqlite3")
        unresolved = True
    for candidate in sorted(root.iterdir()):
        if not JOB_ID.fullmatch(candidate.name):
            continue
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
            report.unknown.append(candidate.name)
            unresolved = True
            continue
        if not candidate.is_dir():
            continue
        path = candidate / "conversation.json"
        if path.is_symlink():
            report.unknown.append(candidate.name)
            unresolved = True
            continue
        if not path.is_file():
            continue
        data: Any = None
        try:
            with path.open("rb") as stream:
                content = stream.read(8_000_001)
            data = json.loads(content) if len(content) <= 8_000_000 else None
            kind = classify_record(data)
        except (OSError, ValueError):
            kind = "unknown"
        if kind == "v2":
            report.v2.append(candidate.name)
        elif kind == "unknown":
            report.unknown.append(candidate.name)
        else:
            report.v1.append(candidate.name)
            candidates.append(candidate)
        try:
            protected.update(
                _record_paths(data, legacy_directory=candidate if kind == "v1" else None)
            )
        except (OSError, ValueError, RuntimeError):
            unresolved = True
    if apply and not unresolved:
        for candidate in candidates:
            if any(
                path.is_relative_to(candidate) or candidate.is_relative_to(path)
                for path in protected
                if path == root or path.is_relative_to(root)
            ):
                continue
            if candidate.is_symlink() or not candidate.resolve(strict=True).is_relative_to(root):
                report.unknown.append(candidate.name)
                continue
            shutil.rmtree(candidate)
            report.deleted.append(candidate.name)
    return report
