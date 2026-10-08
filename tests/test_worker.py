import importlib
import time
from threading import Event

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-worker-token-with-at-least-32-characters"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def worker():
    try:
        return importlib.import_module("flight_agent.worker")
    except ModuleNotFoundError:
        pytest.fail("Authenticated MATLAB worker has not been implemented")


def payload():
    return {
        "filename": "calculation.m",
        "source": "result=params;",
        "kind": "script",
        "parameters": {"value": 3},
        "trusted": True,
        "timeout_seconds": 10,
    }


def wait_job(client, job_id, headers=HEADERS, prefix="/jobs"):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"{prefix}/{job_id}", headers=headers)
        assert response.status_code == 200, response.text
        record = response.json()
        if record["status"] not in ("queued", "running"):
            return record
        time.sleep(0.02)
    pytest.fail("Task did not reach a terminal state")


def test_worker_requires_explicit_enablement(tmp_path, monkeypatch):
    monkeypatch.delenv("MATLAB_ALLOW_TRUSTED_UPLOADS", raising=False)
    with pytest.raises(ValueError, match="MATLAB_ALLOW"):
        worker().create_worker_app(tmp_path, TOKEN)


def test_worker_requires_long_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_UPLOADS", "1")
    with pytest.raises(ValueError, match="token"):
        worker().create_worker_app(tmp_path, "short")


def test_worker_auth_execution_and_artifact_allowlist(tmp_path, fake_engine, monkeypatch):
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_UPLOADS", "1")
    with TestClient(worker().create_worker_app(tmp_path, TOKEN)) as client:
        assert client.post("/jobs", json=payload()).status_code == 401
        response = client.post("/jobs", json=payload(), headers=HEADERS)
        assert response.status_code == 202, response.text
        record = wait_job(client, response.json()["id"])
        assert record["status"] == "completed", record
        assert record["result"] == {"value": 6}
        image = client.get(f"/jobs/{record['id']}/artifacts/figure-1.png", headers=HEADERS)
        assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
        assert (
            client.get(f"/jobs/{record['id']}/artifacts/request.json", headers=HEADERS).status_code
            == 404
        )
        assert client.get("/jobs/" + "a" * 32, headers=HEADERS).status_code == 404


def test_worker_rejects_large_body_before_parsing(tmp_path, monkeypatch):
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_UPLOADS", "1")
    with TestClient(worker().create_worker_app(tmp_path, TOKEN)) as client:
        response = client.post("/jobs", content=b"x" * 2_000_001, headers=HEADERS)
        assert response.status_code == 413


def test_worker_rejects_parallel_execution(tmp_path, monkeypatch):
    module = worker()
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_UPLOADS", "1")
    started, release = Event(), Event()

    def blocked(*args):
        from flight_agent.upload_models import ExecutionOutput

        started.set()
        release.wait(timeout=5)
        return ExecutionOutput(result={"ok": True}, matlab_version="test-engine")

    monkeypatch.setattr(module, "execute_upload", blocked)
    with TestClient(module.create_worker_app(tmp_path, TOKEN)) as client:
        try:
            first = client.post("/jobs", json=payload(), headers=HEADERS)
            assert first.status_code == 202
            assert started.wait(timeout=2)
            assert client.post("/jobs", json=payload(), headers=HEADERS).status_code == 409
        finally:
            release.set()


def test_worker_persists_engine_failure(tmp_path, fake_engine, monkeypatch):
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_UPLOADS", "1")
    monkeypatch.setenv("TEST_ENGINE_MODE", "execution_error")
    with TestClient(worker().create_worker_app(tmp_path, TOKEN)) as client:
        response = client.post("/jobs", json=payload(), headers=HEADERS)
        record = wait_job(client, response.json()["id"])
        assert record["status"] == "failed"
        assert "solver" in record["error"]
        assert record["result"] is None


def test_worker_nan_parameters_return_validation_error_not_server_error(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_UPLOADS", "1")
    data = payload()
    data["parameters"] = {"value": float("nan")}
    with TestClient(worker().create_worker_app(tmp_path, TOKEN)) as client:
        response = client.post(
            "/jobs",
            content=json.dumps(data),
            headers={**HEADERS, "Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert "finite" in response.text
        assert "source" not in response.json()["detail"][0]
