import threading
from datetime import UTC, datetime
from pathlib import Path

from ..json_io import write_json
from ..models import JOB_ID
from ..workbench_contracts import (
    WorkbenchRunRecord,
    WorkbenchRunRequest,
    workbench_request_digest,
)
from .intake import safe_directory


def now() -> str:
    return datetime.now(UTC).isoformat()


class WorkbenchRunStore:
    def __init__(self, root: Path):
        safe_directory(root)
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        for path in self.root.glob("*/status.json"):
            try:
                record = self.get(path.parent.name)
            except KeyError:
                continue
            if record.status in ("queued", "running"):
                self.update(
                    record.id,
                    status="interrupted",
                    stage="interrupted",
                    error="Worker restarted; execution state is unknown. Explicit review required.",
                )

    def directory(self, run_id: str) -> Path:
        if not JOB_ID.fullmatch(run_id):
            raise KeyError("Unknown run")
        path = self.root / run_id
        if path.is_symlink() or not path.is_dir():
            raise KeyError("Unknown run")
        return path

    def create(self, request: WorkbenchRunRequest) -> WorkbenchRunRecord:
        with self.lock:
            directory = self.root / request.id
            directory.mkdir()
            stamp = now()
            record = WorkbenchRunRecord(
                id=request.id,
                request_sha256=workbench_request_digest(request),
                status="queued",
                stage="queued",
                created_at=stamp,
                updated_at=stamp,
                filename=request.filename,
                kind=request.kind,
            )
            write_json(directory / "request.json", request.model_dump(mode="json"))
            write_json(directory / "status.json", record.model_dump(mode="json"))
            return record

    def get(self, run_id: str) -> WorkbenchRunRecord:
        with self.lock:
            path = self.directory(run_id) / "status.json"
            if path.is_symlink() or not path.is_file():
                raise KeyError("Unknown run")
            return WorkbenchRunRecord.model_validate_json(path.read_text("utf-8"))

    def request(self, run_id: str) -> WorkbenchRunRequest:
        path = self.directory(run_id) / "request.json"
        if path.is_symlink():
            raise ValueError("Unsafe run request")
        return WorkbenchRunRequest.model_validate_json(path.read_text("utf-8"))

    def update(self, run_id: str, **changes: object) -> WorkbenchRunRecord:
        with self.lock:
            data = self.get(run_id).model_dump(mode="json")
            data.update(changes)
            data["updated_at"] = now()
            record = WorkbenchRunRecord.model_validate(data)
            write_json(self.directory(run_id) / "status.json", record.model_dump(mode="json"))
            return record

    def recent(self, limit: int = 20) -> list[WorkbenchRunRecord]:
        if limit <= 0:
            return []
        with self.lock:
            records = []
            for path in self.root.glob("*/status.json"):
                try:
                    records.append(self.get(path.parent.name))
                except KeyError:
                    continue
            records.sort(key=lambda record: (record.updated_at, record.id), reverse=True)
            return records[:limit]
