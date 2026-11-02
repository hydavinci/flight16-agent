import pytest
from fastapi.testclient import TestClient

from flight_agent.web_routes.pages import STATIC_ASSETS
from tests.agent_helpers import make_agent_web


def test_web_exposes_separate_agent_and_matlab_workbench_pages(tmp_path):
    app, _, _, _ = make_agent_web(
        tmp_path, [{"action": "answer", "text": "No execution."}],
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        index = client.get("/")
        workbench = client.get("/workbench")
        assert index.status_code == workbench.status_code == 200
        assert workbench.content != index.content
        for name in ("agent.js", "agent.css", "favicon.svg"):
            assert client.get(f"/static/{name}").status_code == 200
        assert "matlab-workbench.js" in STATIC_ASSETS
        assert "matlab-workbench.css" in STATIC_ASSETS
        assert client.get("/static/matlab-workbench.html").status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/jobs"),
        ("POST", "/api/jobs"),
        ("GET", "/api/jobs/" + "a" * 32),
        ("GET", "/api/jobs/" + "a" * 32 + "/result"),
        ("GET", "/api/jobs/" + "a" * 32 + "/artifacts/figure-1.png"),
        ("POST", "/api/jobs/" + "a" * 32 + "/analyze"),
        ("GET", "/api/examples/web_script.m"),
        ("GET", "/static/app.js"),
        ("GET", "/static/style.css"),
        ("GET", "/static/index.html"),
    ],
)
def test_web_rejects_removed_upload_surface(tmp_path, method, path):
    app, _, _, _ = make_agent_web(tmp_path, [])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.get("/api/bootstrap").json()["token"]
        response = client.request(method, path, headers={"x-flight-token": token}, json={})
        assert response.status_code == 404


def test_legacy_jobs_api_remains_absent_with_workbench_enabled(tmp_path):
    app, _, _, _ = make_agent_web(tmp_path, [])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.get("/api/workbench/bootstrap").json()["token"]
        assert client.get(
            "/api/jobs", headers={"x-flight-token": token}
        ).status_code == 404


def test_default_web_needs_no_demo_assets_or_upload_store(tmp_path):
    from flight_agent.web import create_web_app

    with TestClient(create_web_app(tmp_path), base_url="http://127.0.0.1") as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/bootstrap").status_code == 200
        assert not (tmp_path / "runs/web").exists()


def test_web_preserves_generic_auth_origin_and_host_guards(tmp_path):
    app, _, _, _ = make_agent_web(tmp_path, [])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/conversations").status_code == 403
        token = client.get("/api/bootstrap").json()["token"]
        assert client.get(
            "/api/conversations", headers={"x-flight-token": token},
        ).status_code == 200
        assert client.get(
            "/api/bootstrap", headers={"Host": "attacker.example"},
        ).status_code == 400
        assert client.get(
            "/api/bootstrap", headers={"Origin": "https://attacker.example"},
        ).status_code == 403


def test_web_bootstrap_never_returns_worker_or_model_secret(tmp_path, monkeypatch):
    from tests.helpers import TOKEN

    monkeypatch.setenv("MATLAB_WORKER_TOKEN", TOKEN)
    monkeypatch.setenv("LLM_API_KEY", "test-hidden-model-key")
    app, _, _, _ = make_agent_web(tmp_path, [])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        text = client.get("/api/bootstrap").text
        assert TOKEN not in text
        assert "test-hidden-model-key" not in text
