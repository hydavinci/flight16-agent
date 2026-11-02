from threading import Event

from fastapi.testclient import TestClient

from flight_agent.verification.workbench_store import WorkbenchRunStore
from flight_agent.worker import create_worker_app
from tests.helpers import HEADERS, TOKEN, wait_job
from tests.test_verification_matlab import run_request
from tests.test_workbench_matlab import workbench_request
from tests.verification_helpers import project_tree, verification_module


def test_workbench_worker_requires_auth_and_trusted_request(tmp_path, monkeypatch):
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    with TestClient(create_worker_app(tmp_path, TOKEN)) as client:
        request = workbench_request().model_dump(mode="json")
        assert client.post("/workbench/runs", json=request).status_code == 401
        assert client.post(
            "/workbench/runs",
            json={**request, "trusted": False},
            headers=HEADERS,
        ).status_code == 422


def test_workbench_worker_executes_idempotently_and_serves_only_verified_figure(
    tmp_path, fake_engine, monkeypatch
):
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    with TestClient(create_worker_app(tmp_path, TOKEN)) as client:
        request = workbench_request().model_dump(mode="json")
        first = client.post("/workbench/runs", json=request, headers=HEADERS)
        assert first.status_code == 202
        record = wait_job(client, request["id"], prefix="/workbench/runs")
        assert record["status"] == "completed"
        assert record["stage"] == "completed"
        assert record["result"] == {"value": 6}
        assert record["matlab_version"] == "test-engine"
        assert "workbench log" in record["log"]

        repeated = client.post("/workbench/runs", json=request, headers=HEADERS)
        assert repeated.status_code == 202
        assert repeated.json() == record
        changed = {**request, "timeout_seconds": 20}
        assert client.post(
            "/workbench/runs", json=changed, headers=HEADERS
        ).status_code == 409

        prefix = f"/workbench/runs/{request['id']}/artifacts/"
        image = client.get(prefix + "figure-1.png", headers=HEADERS)
        assert image.status_code == 200
        assert image.content.startswith(b"\x89PNG")
        for hidden in (
            "request.json",
            "receipt.json",
            "code/analysis.m",
            "execution/request.json",
            "execution/receipt.json",
            "execution/code/analysis.m",
        ):
            assert client.get(prefix + hidden, headers=HEADERS).status_code == 404

        artifact = (
            tmp_path
            / "runs"
            / "workbench-worker"
            / request["id"]
            / "execution"
            / "figure-1.png"
        )
        artifact.write_bytes(b"corrupted")
        assert client.get(prefix + "figure-1.png", headers=HEADERS).status_code == 409


def test_workbench_worker_recent_runs_are_newest_first_and_limited(tmp_path, monkeypatch):
    root = tmp_path / "runs" / "workbench-worker"
    store = WorkbenchRunStore(root)
    run_ids = [f"{index:032x}" for index in range(21)]
    for run_id in run_ids:
        store.create(workbench_request().model_copy(update={"id": run_id}))
        store.update(
            run_id,
            status="completed",
            stage="completed",
            matlab_version="test-engine",
        )

    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    with TestClient(create_worker_app(tmp_path, TOKEN)) as client:
        response = client.get("/workbench/runs", headers=HEADERS)

    assert response.status_code == 200
    records = response.json()
    assert len(records) == 20
    assert [record["id"] for record in records] == list(reversed(run_ids[1:]))


def test_project_run_blocks_a_different_workbench_run(tmp_path, monkeypatch):
    module = verification_module("worker")
    root = project_tree(tmp_path / "source")
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    started, release = Event(), Event()

    def blocked(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        raise ValueError("controlled numerical failure")

    monkeypatch.setattr(module, "execute_project", blocked)
    with TestClient(create_worker_app(tmp_path, TOKEN)) as client:
        project = run_request(root).model_dump()
        try:
            assert (
                client.post("/verification/runs", json=project, headers=HEADERS).status_code
                == 202
            )
            assert started.wait(timeout=2)
            workbench = workbench_request().model_dump(mode="json")
            assert client.post(
                "/workbench/runs", json=workbench, headers=HEADERS
            ).status_code == 409
        finally:
            release.set()


def test_workbench_run_blocks_a_different_project_run(tmp_path, monkeypatch):
    import flight_agent.verification.workbench_worker as module

    root = project_tree(tmp_path / "source")
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    started, release = Event(), Event()

    def blocked(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        raise ValueError("controlled workbench failure")

    monkeypatch.setattr(module, "execute_workbench", blocked)
    with TestClient(create_worker_app(tmp_path, TOKEN)) as client:
        workbench = workbench_request().model_dump(mode="json")
        try:
            assert client.post(
                "/workbench/runs", json=workbench, headers=HEADERS
            ).status_code == 202
            assert started.wait(timeout=2)
            project = run_request(root).model_dump()
            assert client.post(
                "/verification/runs", json=project, headers=HEADERS
            ).status_code == 409
        finally:
            release.set()
        failed = wait_job(client, workbench["id"], prefix="/workbench/runs")
        assert failed["status"] == "failed"


def test_workbench_background_failure_without_message_is_terminal(tmp_path, monkeypatch):
    import flight_agent.verification.workbench_worker as module

    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")

    def failed(*args, **kwargs):
        raise RuntimeError()

    monkeypatch.setattr(module, "execute_workbench", failed)
    with TestClient(create_worker_app(tmp_path, TOKEN)) as client:
        request = workbench_request().model_dump(mode="json")
        assert client.post("/workbench/runs", json=request, headers=HEADERS).status_code == 202
        record = wait_job(client, request["id"], prefix="/workbench/runs")

    assert record["status"] == "failed"
    assert record["stage"] == "failed"
    assert record["error"] == "RuntimeError"


def test_workbench_scheduling_failure_is_terminal_and_releases_slot(
    tmp_path, fake_engine, monkeypatch
):
    import flight_agent.worker as module

    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    runner = module.BackgroundRunner()
    monkeypatch.setattr(module, "BackgroundRunner", lambda: runner)

    def unavailable(*args, **kwargs):
        raise RuntimeError("Controlled executor shutdown")

    with TestClient(
        module.create_worker_app(tmp_path, TOKEN), raise_server_exceptions=False
    ) as client:
        request = workbench_request().model_dump(mode="json")
        with monkeypatch.context() as patch:
            patch.setattr(runner.pool, "submit", unavailable)
            response = client.post("/workbench/runs", json=request, headers=HEADERS)
        saved = client.get(f"/workbench/runs/{request['id']}", headers=HEADERS).json()
        assert response.status_code == 503
        assert "Controlled executor shutdown" in response.json()["detail"]
        assert saved["status"] == "failed"
        assert saved["stage"] == "failed"
        assert "Controlled executor shutdown" in saved["error"]

        repeated = client.post("/workbench/runs", json=request, headers=HEADERS)
        assert repeated.status_code == 202
        assert repeated.json() == saved
        other = {**request, "id": "4" * 32}
        assert client.post("/workbench/runs", json=other, headers=HEADERS).status_code == 202
        completed = wait_job(client, other["id"], prefix="/workbench/runs")
        assert completed["status"] == "completed"


def test_worker_restart_marks_pending_workbench_run_interrupted(tmp_path, monkeypatch):
    store = WorkbenchRunStore(tmp_path / "runs" / "workbench-worker")
    request = workbench_request()
    store.create(request)

    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    with TestClient(create_worker_app(tmp_path, TOKEN)) as client:
        record = client.get(f"/workbench/runs/{request.id}", headers=HEADERS).json()

    assert record["status"] == "interrupted"
    assert record["stage"] == "interrupted"
    assert "restart" in record["error"].lower()
