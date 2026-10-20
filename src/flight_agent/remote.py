import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

import httpx

from .upload_engine import validate_png
from .upload_models import (
    MAX_FIGURE,
    MAX_LOG,
    MAX_RESPONSE,
    ExecutionOutput,
    JobRecord,
    UploadRequest,
)


class RemoteError(RuntimeError):
    pass


class RemoteWorker:
    def __init__(self, url: str, token: str, client: httpx.Client | None = None):
        parts = urlsplit(url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "MATLAB_WORKER_URL must be an HTTP(S) URL without embedded credentials"
            )
        if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Use HTTPS for remote hosts, or HTTP loopback through an SSH tunnel")
        if len(token) < 32 or not token.isascii():
            raise ValueError("MATLAB_WORKER_TOKEN must contain at least 32 ASCII characters")
        self.url = url.rstrip("/")
        self.token = token
        self.client = client

    @classmethod
    def from_environment(cls) -> Self:
        url = os.environ.get("MATLAB_WORKER_URL", "").strip()
        token = os.environ.get("MATLAB_WORKER_TOKEN", "").strip()
        if not url or not token:
            raise ValueError("Configure MATLAB_WORKER_URL and MATLAB_WORKER_TOKEN first")
        return cls(url, token)

    def _request(
        self,
        client: httpx.Client,
        method: str,
        path: str,
        deadline: float,
        payload: dict[str, object] | None = None,
        limit: int = MAX_RESPONSE,
    ) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RemoteError("Remote deadline exceeded; execution may still be active")
        with client.stream(
            method,
            self.url + path,
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=min(10, remaining),
            follow_redirects=False,
        ) as response:
            if not response.is_success:
                raise RemoteError(
                    f"Worker returned HTTP {response.status_code}; submission not retried"
                )
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > limit:
                    raise RemoteError("Worker response exceeds the size limit")
                if time.monotonic() >= deadline:
                    raise RemoteError("Remote deadline exceeded; execution may still be active")
                chunks.append(chunk)
            return b"".join(chunks)

    def run(
        self,
        directory: Path,
        request: UploadRequest,
        on_submitted: Callable[[str], None],
    ) -> ExecutionOutput:
        client = self.client or httpx.Client(trust_env=False)
        remote_id: str | None = None
        deadline = time.monotonic() + request.timeout_seconds + 30
        try:
            record = JobRecord.model_validate_json(
                self._request(client, "POST", "/jobs", deadline, request.model_dump())
            )
            remote_id = record.id
            on_submitted(remote_id)
            while record.status in ("queued", "running"):
                time.sleep(0.25)
                record = JobRecord.model_validate_json(
                    self._request(client, "GET", f"/jobs/{remote_id}", deadline)
                )
                if record.id != remote_id:
                    raise RemoteError("Worker returned a different job ID")
            directory.mkdir(parents=True, exist_ok=True)
            if len(record.log.encode("utf-8")) > MAX_LOG:
                raise RemoteError("Worker log exceeds size limit")
            (directory / "matlab.log").write_text(record.log, encoding="utf-8")
            if record.status != "completed":
                raise RemoteError(f"Worker {record.status}: {record.error or 'no error detail'}")
            output = ExecutionOutput(
                result=record.result,
                matlab_version=record.matlab_version or "",
                log=record.log,
                figures=record.figures,
            )
            for name in output.figures:
                content = self._request(
                    client, "GET", f"/jobs/{remote_id}/artifacts/{name}", deadline, limit=MAX_FIGURE
                )
                path = directory / name
                path.write_bytes(content)
                validate_png(path)
            (directory / "matlab.log").write_text(output.log, encoding="utf-8")
            (directory / "output.json").write_text(output.model_dump_json(), encoding="utf-8")
            return output
        except (httpx.HTTPError, ValueError, RemoteError) as exc:
            location = remote_id or "unknown (submission acknowledgement not received)"
            raise RemoteError(
                f"{exc}. Remote job: {location}. Execution was not automatically retried "
                "or cancelled; check the worker before resubmitting."
            ) from exc
        finally:
            if self.client is None:
                client.close()
