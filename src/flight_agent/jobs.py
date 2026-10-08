import hashlib
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .session import _write_json
from .upload_models import JOB_ID, JobRecord, UploadRequest

logger = logging.getLogger(__name__)


class BusyError(ValueError):
    pass


class JobStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        for status_file in self.root.glob("*/status.json"):
            record = JobRecord.model_validate_json(status_file.read_text("utf-8"))
            changes: dict[str, object] = {}
            if record.status in ("queued", "running"):
                changes.update(
                    status="interrupted",
                    error="Service restarted; state is unknown. Check worker before retry.",
                )
            if record.agent_status == "running":
                changes.update(agent_status="failed", agent_error="Agent interrupted by restart")
            if changes:
                self.update(record.id, **changes)

    def directory(self, job_id: str) -> Path:
        if not JOB_ID.fullmatch(job_id):
            raise KeyError("Unknown job")
        directory = self.root / job_id
        if directory.is_symlink() or not directory.is_dir():
            raise KeyError("Unknown job")
        return directory

    def create(self, request: UploadRequest, job_id: str | None = None) -> JobRecord:
        with self.lock:
            job_id = job_id or uuid4().hex
            if not JOB_ID.fullmatch(job_id):
                raise ValueError("Invalid job ID")
            directory = self.root / job_id
            directory.mkdir()
            record = JobRecord(
                id=job_id,
                filename=request.filename,
                kind=request.kind,
                created_at=datetime.now(UTC).isoformat(),
                source_sha256=hashlib.sha256(request.source.encode("utf-8")).hexdigest(),
            )
            _write_json(directory / "request.json", request.model_dump())
            _write_json(directory / "status.json", record.model_dump())
            return record

    def get(self, job_id: str) -> JobRecord:
        with self.lock:
            path = self.directory(job_id) / "status.json"
            if path.is_symlink() or not path.is_file():
                raise KeyError("Unknown job")
            return JobRecord.model_validate_json(path.read_text("utf-8"))

    def request(self, job_id: str) -> UploadRequest:
        return UploadRequest.model_validate_json(
            (self.directory(job_id) / "request.json").read_text("utf-8")
        )

    def update(self, job_id: str, **changes: object) -> JobRecord:
        with self.lock:
            data = self.get(job_id).model_dump()
            data.update(changes)
            record = JobRecord.model_validate(data)
            _write_json(self.directory(job_id) / "status.json", record.model_dump())
            return record

    def recent(self) -> list[JobRecord]:
        paths = sorted(
            self.root.glob("*/status.json"), key=lambda path: path.stat().st_mtime, reverse=True
        )[:20]
        return [self.get(path.parent.name) for path in paths]


class JobManager:
    def __init__(self, store: JobStore):
        self.store = store
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="matlab-task")
        self.slot = threading.BoundedSemaphore(1)

    def submit(
        self, request: UploadRequest, operation: Callable[[str], None], job_id: str | None = None
    ) -> JobRecord:
        if not self.slot.acquire(blocking=False):
            raise BusyError("Another task is running; wait for it to finish")
        try:
            record = self.store.create(request, job_id)
            self.pool.submit(self._run, record.id, operation, False)
            return record
        except (OSError, ValueError, RuntimeError):
            self.slot.release()
            raise

    def analyze(self, job_id: str, operation: Callable[[str], None]) -> None:
        if not self.slot.acquire(blocking=False):
            raise BusyError("Another task is running; wait for it to finish")
        try:
            self.store.update(job_id, agent_status="running", agent_error=None)
            self.pool.submit(self._run, job_id, operation, True)
        except (OSError, ValueError, RuntimeError):
            self.slot.release()
            raise

    def _run(self, job_id: str, operation: Callable[[str], None], agent: bool) -> None:
        try:
            if not agent:
                self.store.update(job_id, status="running")
            operation(job_id)
        except Exception as exc:
            # Background failures cannot reach the HTTP caller; persist and log them.
            logger.exception("Task %s failed", job_id)
            if agent:
                self.store.update(job_id, agent_status="failed", agent_error=str(exc)[:4000])
            else:
                self.store.update(job_id, status="failed", error=str(exc)[:4000])
        finally:
            self.slot.release()

    def close(self) -> None:
        self.pool.shutdown(wait=True)
