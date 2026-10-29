import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..models import JOB_ID
from ..runtime.resources import ResourceReference
from .intake import safe_directory

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
    paths = []
    for source in [*sources, *outputs]:
        if source is None:
            continue
        if not isinstance(source, str) or not source or not Path(source).is_absolute():
            raise ValueError("Recorded path ownership is unresolved")
        path = Path(source).resolve(strict=True)
        if (
            source in sources
            or legacy_directory is None
            or not path.is_relative_to(legacy_directory)
        ):
            paths.append(path)
    return paths


def cleanup_legacy_records(root: Path, *, apply: bool) -> CleanupReport:
    safe_directory(root.absolute())
    root = root.resolve(strict=True)
    report = CleanupReport()
    candidates: list[Path] = []
    protected: set[Path] = set()
    unresolved = False
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
            if any(path.is_relative_to(candidate) for path in protected):
                continue
            if candidate.is_symlink() or not candidate.resolve(strict=True).is_relative_to(root):
                report.unknown.append(candidate.name)
                continue
            shutil.rmtree(candidate)
            report.deleted.append(candidate.name)
    return report
