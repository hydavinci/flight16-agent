import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ..session import _write_json
from ..upload_models import JOB_ID
from .contracts import (
    AnalysisRunRequest,
    Conversation,
    ConversationMessage,
    PendingDecision,
    RunRecord,
)
from .intake import safe_directory


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
            _write_json(directory / "request.json", request.model_dump())
            _write_json(directory / "status.json", record.model_dump())
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
            _write_json(self.directory(run_id) / "status.json", record.model_dump())
            return record


class ConversationStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        for path in self.root.glob("*/conversation.json"):
            record = self.get(path.parent.name)
            if record.status not in ("planning", "running"):
                continue
            if record.status == "planning" and record.restore_turn_origin():
                record.messages.append(
                    ConversationMessage(
                        role="assistant",
                        content="服务在处理追问时重启；原任务状态已保留，请重新发送问题或续接任务。",
                        created_at=now(),
                    )
                )
                self.save(record)
                continue
            record.status = "interrupted"
            record.error = "Service restarted; resume saved work without resubmitting a worker run."
            confirmed = record.task is not None and record.task.baseline.confirmed
            record.pending_decision = PendingDecision(
                kind="resume" if confirmed else "clarify", message=record.error
            )
            if record.task:
                for node in record.task.nodes:
                    if node.status == "running":
                        node.status = "interrupted"
                for call in record.task.calls:
                    if call.status == "running":
                        call.status = "interrupted"
                        call.error = record.error
                        call.finished_at = now()
            self.save(record)

    def directory(self, conversation_id: str) -> Path:
        if not JOB_ID.fullmatch(conversation_id):
            raise KeyError("Unknown conversation")
        path = self.root / conversation_id
        if path.is_symlink() or not path.is_dir():
            raise KeyError("Unknown conversation")
        return path

    def create(self) -> Conversation:
        with self.lock:
            stamp = now()
            record = Conversation(id=uuid4().hex, created_at=stamp, updated_at=stamp)
            (self.root / record.id).mkdir()
            self.save(record)
            return record

    def get(self, conversation_id: str) -> Conversation:
        with self.lock:
            path = self.directory(conversation_id) / "conversation.json"
            if path.is_symlink() or not path.is_file():
                raise KeyError("Unknown conversation")
            if path.stat().st_size > 8_000_000:
                raise ValueError("Conversation record exceeds 8 MB")
            record = Conversation.model_validate_json(path.read_text("utf-8"))
            if record.id != conversation_id:
                raise ValueError("Conversation identity mismatch")
            return record

    def save(self, record: Conversation) -> None:
        with self.lock:
            record.updated_at = now()
            _write_json(self.directory(record.id) / "conversation.json", record.model_dump())

    def default_task_root(self, conversation_id: str) -> Path:
        return self.directory(conversation_id) / "tasks"

    def task_directory(
        self,
        conversation_id: str,
        task_id: str,
        output_root: str | None = None,
        *,
        create: bool = False,
    ) -> Path:
        if not JOB_ID.fullmatch(task_id):
            raise ValueError("Invalid task ID")
        internal = self.default_task_root(conversation_id) / task_id
        path = Path(output_root) / f"flight-agent-{task_id}" if output_root else internal
        safe_directory(path)
        if create:
            path.mkdir(parents=True, exist_ok=False)
        elif not path.is_dir():
            raise ValueError("Task result directory is missing")
        return path

    def recent(self) -> list[Conversation]:
        with self.lock:
            paths = sorted(
                self.root.glob("*/conversation.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:20]
            return [self.get(path.parent.name) for path in paths]
