from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.helpers import HEADERS, TOKEN, wait_job, worker
from tests.test_verification_matlab import run_request
from tests.verification_helpers import project_tree, verification_module


@pytest.mark.parametrize("uploads_enabled", [False, True])
def test_worker_keeps_removed_jobs_endpoint_unavailable(tmp_path, monkeypatch, uploads_enabled):
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    if uploads_enabled:
        monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_UPLOADS", "1")
    else:
        monkeypatch.delenv("MATLAB_ALLOW_TRUSTED_UPLOADS", raising=False)
    with TestClient(worker().create_worker_app(tmp_path, TOKEN)) as client:
        assert client.post("/jobs", json={}, headers=HEADERS).status_code == 404
        assert client.get("/jobs/" + "a" * 32, headers=HEADERS).status_code == 404
        assert client.get(
            "/jobs/" + "a" * 32 + "/artifacts/figure-1.png", headers=HEADERS,
        ).status_code == 404


def test_worker_health_reports_project_and_single_file_execution(tmp_path, monkeypatch):
    from flight_agent.matlab import engine_available

    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    with TestClient(worker().create_worker_app(tmp_path, TOKEN)) as client:
        assert client.get("/health").status_code == 401
        assert client.get("/health", headers=HEADERS).json() == {
            "service": "matlab-worker",
            "engine_package": engine_available(),
            "trusted_code_only": True,
            "project_execution": True,
            "single_file_execution": True,
        }


def test_worker_requires_projects_even_with_old_upload_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_UPLOADS", "1")
    monkeypatch.delenv("MATLAB_ALLOW_TRUSTED_PROJECTS", raising=False)
    with pytest.raises(ValueError, match="MATLAB_ALLOW_TRUSTED_PROJECTS=1"):
        worker().create_worker_app(tmp_path, TOKEN)


@pytest.mark.parametrize("token", ["short", "密" * 32])
def test_project_worker_requires_long_ascii_token(tmp_path, monkeypatch, token):
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    with pytest.raises(ValueError, match="token"):
        worker().create_worker_app(tmp_path, token)


def test_project_worker_rejects_large_body_before_parsing(tmp_path, monkeypatch):
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    with TestClient(worker().create_worker_app(tmp_path, TOKEN)) as client:
        response = client.post(
            "/verification/runs", content=b"x" * 2_000_001, headers=HEADERS,
        )
        assert response.status_code == 413


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.5:9001",
        "ftp://worker.example",
        "https://",
        "https://user:password@worker.example",
        "https://worker.example?token=hidden",
        "https://worker.example#fragment",
    ],
)
def test_remote_project_transport_rejects_unsafe_destination(url):
    backend_type = verification_module("backend").RemoteSimulinkBackend
    with pytest.raises(ValueError):
        backend_type(url, TOKEN)


def test_remote_project_transport_does_not_retry_rejected_authorization():
    from flight_agent.remote import RemoteError

    backend_type = verification_module("backend").RemoteSimulinkBackend
    requests = []

    def rejected(request):
        requests.append(request)
        return httpx.Response(401)

    with httpx.Client(transport=httpx.MockTransport(rejected)) as client:
        backend = backend_type("http://127.0.0.1:9001", TOKEN, client=client)
        with pytest.raises(RemoteError, match="401"):
            backend.inspect("/source")
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == HEADERS["Authorization"]


def test_project_worker_can_be_enabled_without_uploads(tmp_path, monkeypatch):
    monkeypatch.delenv("MATLAB_ALLOW_TRUSTED_UPLOADS", raising=False)
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    with TestClient(worker().create_worker_app(tmp_path, TOKEN)) as client:
        assert client.get("/health", headers=HEADERS).status_code == 200
        assert client.post("/jobs", json={}, headers=HEADERS).status_code == 404


def test_project_routes_require_auth_and_explicit_enablement(tmp_path, monkeypatch):
    root = project_tree(tmp_path / "source")
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    with TestClient(worker().create_worker_app(tmp_path, TOKEN)) as client:
        data = {"root": str(root)}
        assert client.post("/verification/projects/inspect", json=data).status_code == 401
        assert (
            client.post("/verification/projects/inspect", json=data, headers=HEADERS).status_code
            == 200
        )
        assert (
            client.post(
                "/verification/runs", json=run_request(root).model_dump()
            ).status_code
            == 401
        )


def test_worker_inspection_idempotency_and_verified_artifacts(tmp_path, fake_engine, monkeypatch):
    root = project_tree(tmp_path / "source")
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    with TestClient(worker().create_worker_app(tmp_path, TOKEN)) as client:
        inspected = client.post(
            "/verification/projects/inspect", json={"root": str(root)}, headers=HEADERS
        )
        assert inspected.status_code == 200, inspected.text
        request = run_request(root).model_dump()
        response = client.post("/verification/runs", json=request, headers=HEADERS)
        assert response.status_code == 202, response.text
        record = wait_job(client, request["id"], prefix="/verification/runs")
        assert record["status"] == "completed", record
        repeated = client.post("/verification/runs", json=request, headers=HEADERS)
        assert repeated.status_code == 202
        assert repeated.json()["result"] == record["result"]
        changed = {**request, "timeout_seconds": 20}
        assert client.post("/verification/runs", json=changed, headers=HEADERS).status_code == 409
        path = f"/verification/runs/{request['id']}/artifacts/"
        image = client.get(path + "step-response.png", headers=HEADERS)
        assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
        assert client.get(path + "request.json", headers=HEADERS).status_code == 404
        assert client.get(path + "project/initialize.m", headers=HEADERS).status_code == 404
        artifact = tmp_path / "runs/verification-worker" / request["id"] / "step-response.png"
        artifact.write_bytes(b"corrupted")
        assert client.get(path + "step-response.png", headers=HEADERS).status_code == 409


def test_project_worker_rejects_untrusted_requests(tmp_path, monkeypatch):
    root = project_tree(tmp_path / "source")
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    with TestClient(worker().create_worker_app(tmp_path, TOKEN)) as client:
        request = {**run_request(root).model_dump(), "trusted": False}
        assert client.post("/verification/runs", json=request, headers=HEADERS).status_code == 422


def test_project_runs_share_one_execution_slot(tmp_path, monkeypatch):
    module = verification_module("worker")
    root = project_tree(tmp_path / "source")
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    started, release = Event(), Event()

    def blocked(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        raise ValueError("controlled numerical failure")

    monkeypatch.setattr(module, "execute_project", blocked)
    with TestClient(worker().create_worker_app(tmp_path, TOKEN)) as client:
        try:
            request = run_request(root).model_dump()
            assert (
                client.post("/verification/runs", json=request, headers=HEADERS).status_code == 202
            )
            assert started.wait(timeout=2)
            repeated = client.post("/verification/runs", json=request, headers=HEADERS)
            assert repeated.status_code == 202
            other = {**request, "id": "4" * 32}
            assert client.post("/verification/runs", json=other, headers=HEADERS).status_code == 409
        finally:
            release.set()
        record = wait_job(client, request["id"], prefix="/verification/runs")
        assert record["status"] == "failed"
        assert "controlled numerical failure" in record["error"]


def test_worker_scheduling_failure_is_terminal_and_releases_execution_slot(
    tmp_path, fake_engine, monkeypatch
):
    module = worker()
    root = project_tree(tmp_path / "source")
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    runner = module.BackgroundRunner()
    monkeypatch.setattr(module, "BackgroundRunner", lambda: runner)

    def unavailable(*args, **kwargs):
        raise RuntimeError("Controlled executor shutdown")

    with TestClient(
        module.create_worker_app(tmp_path, TOKEN), raise_server_exceptions=False
    ) as client:
        request = run_request(root).model_dump()
        with monkeypatch.context() as patch:
            patch.setattr(runner.pool, "submit", unavailable)
            response = client.post("/verification/runs", json=request, headers=HEADERS)
        saved = client.get(f"/verification/runs/{request['id']}", headers=HEADERS).json()
        assert saved["status"] == "failed", saved
        assert saved["stage"] == "failed"
        assert "Controlled executor shutdown" in saved["error"]
        assert response.status_code == 503, response.text
        assert "Controlled executor shutdown" in response.json()["detail"]
        repeated = client.post("/verification/runs", json=request, headers=HEADERS)
        assert repeated.status_code == 202
        assert repeated.json() == saved
        other = {**request, "id": "4" * 32}
        assert client.post("/verification/runs", json=other, headers=HEADERS).status_code == 202
        completed = wait_job(client, other["id"], prefix="/verification/runs")
        assert completed["status"] == "completed", completed


def test_worker_restart_marks_pending_run_interrupted_not_replayed(tmp_path):
    store_type = verification_module("store").WorkerRunStore
    root = project_tree(tmp_path / "source")
    request = run_request(root)
    store = store_type(tmp_path / "worker")
    store.create(request)
    recovered = store_type(tmp_path / "worker")
    assert recovered.get(request.id).status == "interrupted"
    assert recovered.request(request.id) == request
    assert "restart" in recovered.get(request.id).error.lower()


def test_remote_backend_transfers_artifacts_and_reconciles_without_resubmission(
    tmp_path, fake_engine, monkeypatch
):
    backend_type = verification_module("backend").RemoteSimulinkBackend
    root = project_tree(tmp_path / "source")
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    with TestClient(worker().create_worker_app(tmp_path, TOKEN)) as client:
        backend = backend_type("http://127.0.0.1", TOKEN, client=client)
        assert backend.inspect(str(root)).root == str(root)
        request = run_request(root)
        stages = []
        result = backend.execute(request, tmp_path / "local", stages.append)
        assert result.run_id == request.id
        assert result.artifacts
        assert (tmp_path / "local/step-response.png").exists()
        assert stages[-1].status == "completed"
        module = verification_module("worker")

        def forbidden(*args, **kwargs):
            pytest.fail("Reconciliation must not execute MATLAB again")

        monkeypatch.setattr(module, "execute_project", forbidden)
        recovered = backend.execute(request, tmp_path / "recovered", stages.append, resume=True)
        assert recovered == result
        assert (tmp_path / "recovered/analysis.json").exists()


def test_remote_reconcile_of_unknown_run_is_not_a_new_submission(tmp_path, monkeypatch):
    backend_type = verification_module("backend").RemoteSimulinkBackend
    from flight_agent.remote import RemoteError

    root = project_tree(tmp_path / "source")
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    with TestClient(worker().create_worker_app(tmp_path, TOKEN)) as client:
        backend = backend_type("http://127.0.0.1", TOKEN, client=client)
        with pytest.raises(RemoteError, match="404"):
            backend.execute(run_request(root), tmp_path / "local", lambda _: None, resume=True)
        assert not list((tmp_path / "runs/verification-worker").glob("*/status.json"))
