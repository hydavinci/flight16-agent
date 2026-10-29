import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..upload_models import JOB_ID
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
    if data.get("schema_version") == "2.0":
        return "v2"
    tasks = [
        data.get("task"),
        *(data.get("archived_tasks", []) if isinstance(data.get("archived_tasks"), list) else []),
    ]
    concrete = [item for item in tasks if isinstance(item, dict)]
    if concrete and all(item.get("schema_version") == "2.0" for item in concrete):
        return "v2"
    if any(item.get("scenario") in ("d1", "d2", "general") for item in concrete):
        return "v1"
    return "unknown"


def normalize_v2_record(data: dict[str, object]) -> dict[str, object]:
    normalized = dict(data)
    for name in ("scenario", "goal", "active_workflow", "workflow_agenda"):
        normalized.pop(name, None)
    normalized["schema_version"] = "2.0"
    return normalized


def cleanup_legacy_records(root: Path, *, apply: bool) -> CleanupReport:
    safe_directory(root.absolute())
    root = root.resolve(strict=True)
    report = CleanupReport()
    for candidate in sorted(root.iterdir()):
        if not JOB_ID.fullmatch(candidate.name):
            continue
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
            report.unknown.append(candidate.name)
            continue
        if not candidate.is_dir():
            continue
        path = candidate / "conversation.json"
        if path.is_symlink():
            report.unknown.append(candidate.name)
            continue
        if not path.is_file():
            continue
        try:
            with path.open("rb") as stream:
                content = stream.read(8_000_001)
            kind = classify_record(json.loads(content)) if len(content) <= 8_000_000 else "unknown"
        except (OSError, ValueError):
            kind = "unknown"
        if kind == "v2":
            report.v2.append(candidate.name)
        elif kind == "unknown":
            report.unknown.append(candidate.name)
        else:
            report.v1.append(candidate.name)
            if apply:
                if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
                    report.unknown.append(candidate.name)
                    continue
                shutil.rmtree(candidate)
                report.deleted.append(candidate.name)
    return report
