import os
import time
from typing import Self
from urllib.parse import urlsplit

import httpx


class RemoteError(RuntimeError):
    pass


class RemoteClient:
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
        *,
        limit: int = 8_000_000,
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
