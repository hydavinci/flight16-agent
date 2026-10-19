import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlencode

import httpx

from ..directories import DirectoryListing
from ..remote import RemoteError, RemoteWorker
from ..session import _write_json
from .artifacts import MAX_ARTIFACT_BYTES, verify_artifact
from .contracts import AnalysisResult, AnalysisRunRequest, ProjectDescriptor, RunRecord
from .store import request_digest


class AnalysisBackend(Protocol):
    def inspect(self, root: str) -> ProjectDescriptor: ...

    def execute(
        self,
        request: AnalysisRunRequest,
        directory: Path,
        on_progress: Callable[[RunRecord], None],
        *,
        resume: bool = False,
    ) -> AnalysisResult: ...


class RemoteSimulinkBackend(RemoteWorker):
    def browse(self, path: str = "") -> DirectoryListing:
        client = self.client or httpx.Client(trust_env=False)
        try:
            return DirectoryListing.model_validate_json(
                self._request(
                    client,
                    "GET",
                    "/verification/directories?" + urlencode({"path": path}),
                    time.monotonic() + 30,
                    limit=4_000_000,
                )
            )
        except (httpx.HTTPError, ValueError, RemoteError) as exc:
            raise RemoteError(f"Directory browsing failed: {exc}") from exc
        finally:
            if self.client is None:
                client.close()

    def inspect(self, root: str) -> ProjectDescriptor:
        client = self.client or httpx.Client(trust_env=False)
        try:
            return ProjectDescriptor.model_validate_json(
                self._request(
                    client,
                    "POST",
                    "/verification/projects/inspect",
                    time.monotonic() + 30,
                    {"root": root},
                    limit=4_000_000,
                )
            )
        except (httpx.HTTPError, ValueError) as exc:
            raise RemoteError(f"Project inspection failed: {exc}") from exc
        finally:
            if self.client is None:
                client.close()

    def execute(
        self,
        request: AnalysisRunRequest,
        directory: Path,
        on_progress: Callable[[RunRecord], None],
        *,
        resume: bool = False,
    ) -> AnalysisResult:
        client = self.client or httpx.Client(trust_env=False)
        deadline = time.monotonic() + request.timeout_seconds + 60
        resource = f"/verification/runs/{request.id}"
        try:
            record = RunRecord.model_validate_json(
                self._request(
                    client,
                    "GET" if resume else "POST",
                    resource if resume else "/verification/runs",
                    deadline,
                    None if resume else request.model_dump(),
                    limit=8_000_000,
                )
            )
            while True:
                if record.id != request.id or record.request_sha256 != request_digest(request):
                    raise RemoteError("Worker run identity does not match the requested input")
                on_progress(record)
                if record.status not in ("queued", "running"):
                    break
                time.sleep(0.25)
                record = RunRecord.model_validate_json(
                    self._request(client, "GET", resource, deadline, limit=8_000_000)
                )
            directory.mkdir(parents=True, exist_ok=True)
            if len(record.log.encode("utf-8")) > 4_000_000:
                raise RemoteError("Worker log exceeds 4 MB")
            (directory / "matlab.log").write_text(record.log, encoding="utf-8")
            if record.status != "completed" or record.result is None:
                raise RemoteError(f"Worker {record.status}: {record.error or 'missing result'}")
            result = record.result
            if (
                result.run_id != request.id
                or result.project_fingerprint != request.project_fingerprint
                or result.trim_point != request.trim_point
                or {item.kind for item in result.analyses} != set(request.analyses)
            ):
                raise RemoteError("Worker result context does not match the confirmed request")
            for item in result.artifacts:
                path = directory / item.path
                if directory.resolve() not in path.resolve().parents or any(
                    part.is_symlink()
                    for part in (path, *path.parents)
                    if part != directory and directory in part.parents
                ):
                    raise RemoteError("Unsafe local artifact destination")
                content = self._request(
                    client,
                    "GET",
                    resource + "/artifacts/" + quote(item.path, safe="/"),
                    deadline,
                    limit=min(MAX_ARTIFACT_BYTES, item.size),
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                verify_artifact(directory, item)
            _write_json(directory / "analysis.json", result.model_dump())
            return result
        except (httpx.HTTPError, OSError, ValueError, RemoteError) as exc:
            raise RemoteError(
                f"{exc}. Run {request.id} was not automatically retried. "
                "Resume reconciles this ID without starting another simulation."
            ) from exc
        finally:
            if self.client is None:
                client.close()
