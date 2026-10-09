import importlib
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.helpers import ScriptedModel, serve_app, successful_responses
from tests.test_worker import TOKEN, payload, wait_job


def web_module():
    try:
        return importlib.import_module("flight_agent.web")
    except ModuleNotFoundError:
        pytest.fail("Web upload API has not been implemented")


@pytest.fixture
def remote_worker(project, fake_engine, monkeypatch):
    from flight_agent.worker import create_worker_app

    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_UPLOADS", "1")
    monkeypatch.setenv("MATLAB_WORKER_TOKEN", TOKEN)
    with serve_app(create_worker_app(project, TOKEN)) as url:
        monkeypatch.setenv("MATLAB_WORKER_URL", url)
        yield url


def headers(client):
    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    return {"X-Flight-Token": bootstrap.json()["token"]}


def test_web_rejects_cross_origin_unknown_host_and_missing_token(project):
    with TestClient(web_module().create_web_app(project), base_url="http://127.0.0.1") as client:
        assert client.post("/api/jobs", json=payload()).status_code == 403
        assert client.get("/api/bootstrap", headers={"Host": "attacker.example"}).status_code == 400
        assert (
            client.get("/api/bootstrap", headers={"Origin": "https://attacker.example"}).status_code
            == 403
        )
        assert client.get("/api/jobs", headers=headers(client)).status_code == 200


def test_web_bootstrap_never_returns_worker_or_model_secret(project, monkeypatch):
    monkeypatch.setenv("MATLAB_WORKER_TOKEN", TOKEN)
    monkeypatch.setenv("LLM_API_KEY", "test-hidden-model-key")
    with TestClient(web_module().create_web_app(project), base_url="http://127.0.0.1") as client:
        text = client.get("/api/bootstrap").text
        assert TOKEN not in text
        assert "test-hidden-model-key" not in text


@pytest.mark.parametrize(
    "result",
    [
        {"value": 9007199254740993, "nested": [-9007199254740993, "中文", None]},
        9007199254740993,
        None,
    ],
)
def test_web_result_json_preserves_saved_numbers(project, result):
    from flight_agent.jobs import JobStore
    from flight_agent.upload_models import UploadRequest

    store = JobStore(project / "runs" / "web")
    record = store.create(UploadRequest.model_validate(payload()))
    store.update(record.id, status="completed", result=result)
    path = f"/api/jobs/{record.id}/result"
    with TestClient(web_module().create_web_app(project), base_url="http://127.0.0.1") as client:
        assert client.get(path).status_code == 403
        auth = headers(client)
        response = client.get(path, headers=auth)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.headers["content-disposition"] == 'attachment; filename="result.json"'
        assert json.loads(response.content) == result
        assert client.get(f"/api/jobs/{'a' * 32}/result", headers=auth).status_code == 404
        store.update(record.id, status="running")
        assert client.get(path, headers=auth).status_code == 409


def test_unconfigured_worker_fails_without_fake_results(project, monkeypatch):
    monkeypatch.delenv("MATLAB_WORKER_URL", raising=False)
    monkeypatch.delenv("MATLAB_WORKER_TOKEN", raising=False)
    with TestClient(web_module().create_web_app(project), base_url="http://127.0.0.1") as client:
        auth = headers(client)
        response = client.post("/api/jobs", json=payload(), headers=auth)
        assert response.status_code == 503
        assert "MATLAB_WORKER" in response.text


def test_web_executes_on_remote_worker_and_displays_generic_result(project, remote_worker):
    with TestClient(web_module().create_web_app(project), base_url="http://127.0.0.1") as client:
        auth = headers(client)
        response = client.post("/api/jobs", json=payload(), headers=auth)
        assert response.status_code == 202, response.text
        record = wait_job(client, response.json()["id"], auth, "/api/jobs")
        assert record["status"] == "completed", record
        assert record["result"] == {"value": 6}
        assert record["remote_id"]
        image = client.get(f"/api/jobs/{record['id']}/artifacts/figure-1.png", headers=auth)
        assert image.status_code == 200
        assert image.content.startswith(b"\x89PNG")
        analyzed = client.post(
            f"/api/jobs/{record['id']}/analyze", json={"request": "分析"}, headers=auth
        )
        assert analyzed.status_code == 202
        record = wait_analysis(client, record["id"], auth)
        assert record["agent_status"] == "undetermined"
        assert record["assessment"]["status"] == "undetermined"
        assert record["result"] == {"value": 6}


def wait_analysis(client, job_id, auth):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        record = client.get(f"/api/jobs/{job_id}", headers=auth).json()
        if record["agent_status"] != "running":
            return record
        time.sleep(0.02)
    pytest.fail("Agent analysis never finished")


def test_web_agent_uses_actual_upload_provenance(
    project, remote_worker, parameter_data, result_data, monkeypatch
):
    module = web_module()
    monkeypatch.setenv("TEST_ENGINE_MODE", "upload_metrics")
    monkeypatch.setenv("TEST_METRICS_JSON", json.dumps(result_data))
    monkeypatch.setattr(
        module, "create_model", lambda: ScriptedModel(responses=successful_responses())
    )
    data = payload()
    data["parameters"] = parameter_data
    data["source"] = "% Actual uploaded source\nresult = params;"
    with TestClient(module.create_web_app(project), base_url="http://127.0.0.1") as client:
        auth = headers(client)
        response = client.post("/api/jobs", json=data, headers=auth)
        record = wait_job(client, response.json()["id"], auth, "/api/jobs")
        assert record["status"] == "completed", record
        monkeypatch.setenv("TEST_ENGINE_MODE", "execution_error")
        response = client.post(
            f"/api/jobs/{record['id']}/analyze", json={"request": "分析"}, headers=auth
        )
        assert response.status_code == 202
        analyzed = wait_analysis(client, record["id"], auth)
        assert analyzed["agent_status"] == "completed", analyzed
        assert analyzed["assessment"]["status"] == "pass"
        agent_dir = project / "runs" / analyzed["agent_run_id"]
        assert (agent_dir / "matlab_source.m").read_text() == data["source"]
        assert (
            json.loads((agent_dir / "metadata.json").read_text())["matlab_entry"] == "calculation"
        )
        assert "[KB:demo.md]" in analyzed["report"]
        assert len(list((project / "runs" / "worker").glob("*/status.json"))) == 1


def test_web_preserves_engine_result_if_model_unconfigured(
    project, remote_worker, parameter_data, result_data, monkeypatch
):
    monkeypatch.setenv("TEST_ENGINE_MODE", "upload_metrics")
    monkeypatch.setenv("TEST_METRICS_JSON", json.dumps(result_data))
    monkeypatch.delenv("LLM_MODEL", raising=False)
    data = payload()
    data["parameters"] = parameter_data
    with TestClient(web_module().create_web_app(project), base_url="http://127.0.0.1") as client:
        auth = headers(client)
        record = wait_job(
            client,
            client.post("/api/jobs", json=data, headers=auth).json()["id"],
            auth,
            "/api/jobs",
        )
        client.post(f"/api/jobs/{record['id']}/analyze", json={"request": "分析"}, headers=auth)
        record = wait_analysis(client, record["id"], auth)
        assert record["status"] == "completed"
        assert record["agent_status"] == "failed"
        assert record["result"] == result_data


def test_remote_rejects_bad_auth_without_retry(tmp_path):
    try:
        module = importlib.import_module("flight_agent.remote")
    except ModuleNotFoundError:
        pytest.fail("Remote worker client has not been implemented")
    from flight_agent.upload_models import UploadRequest

    calls = []

    def reject(request):
        calls.append(request)
        return httpx.Response(401, json={"detail": "Unauthorized"})

    with httpx.Client(transport=httpx.MockTransport(reject)) as client:
        runner = module.RemoteWorker("http://127.0.0.1:9001", TOKEN, client=client)
        with pytest.raises(module.RemoteError, match="401"):
            runner.run(tmp_path, UploadRequest.model_validate(payload()), lambda job_id: None)
    assert len(calls) == 1


def test_remote_rejects_nonloopback_plain_http():
    try:
        module = importlib.import_module("flight_agent.remote")
    except ModuleNotFoundError:
        pytest.fail("Remote worker client has not been implemented")
    with pytest.raises(ValueError, match="HTTPS"):
        module.RemoteWorker("http://192.168.1.5:9001", TOKEN)


def test_remote_accepts_valid_large_result_with_escaped_log(tmp_path):
    from flight_agent.remote import RemoteWorker
    from flight_agent.upload_models import ExecutionOutput, JobRecord, UploadRequest

    output = ExecutionOutput(
        result="x" * 3_900_000, log="\x1b[" * 500_000, matlab_version="test-engine"
    )
    record = JobRecord(
        id="a" * 32,
        filename="calculation.m",
        kind="script",
        created_at="2026-10-08T00:00:00Z",
        source_sha256="b" * 64,
        status="completed",
        **output.model_dump(),
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=record.model_dump()))
    ) as client:
        result = RemoteWorker("http://127.0.0.1:9001", TOKEN, client).run(
            tmp_path, UploadRequest.model_validate(payload()), lambda job_id: None
        )
    assert result.result == output.result
    assert result.log == output.log
