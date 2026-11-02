import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from ..json_io import write_json
from ..models import JOB_ID
from .contracts import AnalysisRunRequest, RunRecord
from .sqlite_store import SQLiteConversationStore as ConversationStore  # noqa: F401


def now() -> str:
    return datetime.now(UTC).isoformat()


def request_digest(request: AnalysisRunRequest) -> str:
    content = json.dumps(request.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class WorkerRunStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        for path in self.root.glob("*/status.json"):
            record = self.get(path.parent.name)
            if record.status in ("queued", "running"):
                self.update(
                    record.id,
                    status="interrupted",
                    error="Worker restarted; execution state is unknown. Explicit review required.",
                )

    def directory(self, run_id: str) -> Path:
        if not JOB_ID.fullmatch(run_id):
            raise KeyError("Unknown run")
        path = self.root / run_id
        if path.is_symlink() or not path.is_dir():
            raise KeyError("Unknown run")
        return path

    def create(self, request: AnalysisRunRequest) -> RunRecord:
        with self.lock:
            directory = self.root / request.id
            directory.mkdir()
            stamp = now()
            record = RunRecord(
                id=request.id,
                request_sha256=request_digest(request),
                created_at=stamp,
                updated_at=stamp,
            )
            write_json(directory / "request.json", request.model_dump())
            write_json(directory / "status.json", record.model_dump())
            return record

    def get(self, run_id: str) -> RunRecord:
        with self.lock:
            path = self.directory(run_id) / "status.json"
            if path.is_symlink() or not path.is_file():
                raise KeyError("Unknown run")
            return RunRecord.model_validate_json(path.read_text("utf-8"))

    def request(self, run_id: str) -> AnalysisRunRequest:
        path = self.directory(run_id) / "request.json"
        if path.is_symlink():
            raise ValueError("Unsafe run request")
        return AnalysisRunRequest.model_validate_json(path.read_text("utf-8"))

    def update(self, run_id: str, **changes: object) -> RunRecord:
        with self.lock:
            data = self.get(run_id).model_dump()
            data.update(changes)
            data["updated_at"] = now()
            record = RunRecord.model_validate(data)
            write_json(self.directory(run_id) / "status.json", record.model_dump())
            return record
