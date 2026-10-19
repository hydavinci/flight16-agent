from pathlib import Path

from fastapi.testclient import TestClient

from flight_agent.verification.backend import RemoteSimulinkBackend
from flight_agent.web import create_web_app
from tests.test_diagnosis_runtime import runtime_fixture
from tests.test_verification_web import API, artifact_url, confirm, wait_conversation
from tests.test_web import headers


def test_d2_api_confirmation_reports_and_history_share_d1_routes(project, tmp_path):
    runtime, _, backend, root = runtime_fixture(tmp_path)
    with TestClient(
        create_web_app(project, verification_runtime=runtime), base_url="http://127.0.0.1"
    ) as client:
        auth = headers(client)
        response = client.post(
            API, headers=auth,
            json={"scenario": "d2", "project_root": str(root), "message": "Diagnose mismatch."},
        )
        assert response.status_code == 202, response.text
        pending = wait_conversation(client, response.json()["id"], auth)
        assert pending["scenario"] == pending["task"]["scenario"] == "d2"
        assert pending["pending_decision"]["kind"] == "confirm_baseline"
        assert not pending["task"]["analysis_runs"]
        for action in ("retry", "replan"):
            assert client.post(
                f"{API}/{pending['id']}/{action}", headers=auth,
                json={"version": 1, "trusted": True},
            ).status_code == 409
        completed = confirm(client, auth, pending)
        assert completed["status"] == "completed", completed
        assert completed["task"]["termination"]["reason"] == "evidence_sufficient"
        assert len(completed["task"]["analysis_runs"]) == 2
        assert client.get(API, headers=auth).json()[0]["scenario"] == "d2"
        report = next(
            item for item in completed["task"]["artifacts"] if item["path"].endswith(".docx")
        )
        assert client.get(artifact_url(completed, report), headers=auth).status_code == 200
        (Path(completed["task"]["result_directory"]) / report["path"]).write_bytes(b"tampered")
        assert client.get(artifact_url(completed, report), headers=auth).status_code == 409
        assert not backend.calls


def test_d2_directory_browser_uses_agent_host_without_a_worker(project, tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("D2 directory selection must not contact a MATLAB Worker")

    monkeypatch.setattr(RemoteSimulinkBackend, "from_environment", forbidden)
    runtime, _, _, root = runtime_fixture(tmp_path)
    with TestClient(
        create_web_app(project, verification_runtime=runtime), base_url="http://127.0.0.1"
    ) as client:
        auth = headers(client)
        response = client.get(
            "/api/directories",
            params={"target": "project", "scenario": "d2", "path": str(root)}, headers=auth,
        )
        assert response.status_code == 200, response.text
        assert response.json()["path"] == str(root)


def test_d2_native_data_picker_needs_no_worker_and_still_requires_explicit_enablement(
    project, tmp_path, monkeypatch
):
    from flight_agent import web

    runtime, _, _, root = runtime_fixture(tmp_path)
    calls = []
    monkeypatch.delenv("MATLAB_WORKER_URL", raising=False)
    monkeypatch.delenv("MATLAB_WORKER_TOKEN", raising=False)

    def choose(path, *, title):
        calls.append((path, title))
        return str(root)

    monkeypatch.setattr(web, "choose_native_directory", choose)
    with TestClient(
        create_web_app(project, verification_runtime=runtime, native_directory_picker=True),
        base_url="http://127.0.0.1",
    ) as client:
        response = client.post(
            "/api/directories/select", headers=headers(client),
            json={"target": "project", "scenario": "d2", "path": str(root)},
        )
        assert response.status_code == 200, response.text
        assert calls == [(str(root), "选择诊断数据目录")]
        assert not runtime.store.recent()
