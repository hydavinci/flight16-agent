from contextlib import contextmanager
from pathlib import Path
from threading import Event
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from flight_agent.remote import RemoteClient, RemoteError
from flight_agent.web import create_web_app
from flight_agent.web_routes.workbench import workbench_router
from flight_agent.workbench_contracts import WorkbenchRunRequest
from tests.helpers import TOKEN, serve_app, wait_job
from tests.test_workbench_matlab import workbench_request

ROOT = Path(__file__).resolve().parents[1]


def record_data(**changes):
    return {
        "id": "3" * 32,
        "request_sha256": "2" * 64,
        "status": "queued",
        "stage": "queued",
        "created_at": "2026-11-02T00:00:00Z",
        "updated_at": "2026-11-02T00:00:00Z",
        "filename": "analysis.m",
        "kind": "script",
        "matlab_version": None,
        "result": None,
        "error": None,
        "log": "",
        "artifacts": [],
        **changes,
    }


def test_remote_workbench_methods_use_authorized_bounded_worker_requests():
    seen = []

    def worker(request):
        seen.append(request)
        if request.url.path.endswith("/artifacts/figure-1.png"):
            return httpx.Response(200, content=b"\x89PNG\r\n")
        if request.method == "POST":
            return httpx.Response(202, json=record_data())
        if request.url.path == "/workbench/runs":
            return httpx.Response(200, json=[record_data()])
        return httpx.Response(200, json=record_data())

    request = WorkbenchRunRequest.model_validate(
        workbench_request().model_dump(mode="json")
    )
    with httpx.Client(transport=httpx.MockTransport(worker)) as client:
        remote = RemoteClient("http://127.0.0.1:9001", TOKEN, client=client)
        assert remote.submit_workbench(request).id == request.id
        assert remote.recent_workbench()[0].id == request.id
        assert remote.workbench_status(request.id).id == request.id
        assert remote.workbench_artifact(request.id, "figure-1.png").startswith(b"\x89PNG")

    assert [item.method for item in seen] == ["POST", "GET", "GET", "GET"]
    assert [item.url.path for item in seen] == [
        "/workbench/runs",
        "/workbench/runs",
        f"/workbench/runs/{request.id}",
        f"/workbench/runs/{request.id}/artifacts/figure-1.png",
    ]
    assert all(item.headers["Authorization"] == f"Bearer {TOKEN}" for item in seen)


def test_remote_workbench_preserves_conflict_without_resubmitting():
    requests = []

    def conflict(request):
        requests.append(request)
        return httpx.Response(409)

    with httpx.Client(transport=httpx.MockTransport(conflict)) as client:
        remote = RemoteClient("http://127.0.0.1:9001", TOKEN, client=client)
        with pytest.raises(RemoteError) as caught:
            remote.submit_workbench(workbench_request())

    assert getattr(caught.value, "status_code", None) == 409
    assert len(requests) == 1


@pytest.mark.parametrize("failure", ["authorization", "transport"])
def test_remote_workbench_never_retries_unacknowledged_post(failure):
    requests = []

    def failed(request):
        requests.append(request)
        if failure == "authorization":
            return httpx.Response(401)
        raise httpx.ConnectError("controlled disconnect", request=request)

    with httpx.Client(transport=httpx.MockTransport(failed)) as client:
        remote = RemoteClient("http://127.0.0.1:9001", TOKEN, client=client)
        with pytest.raises((RemoteError, httpx.HTTPError)):
            remote.submit_workbench(workbench_request())

    assert len(requests) == 1


def test_remote_workbench_bounds_json_and_artifact_responses():
    def oversized(request):
        if "/artifacts/" in request.url.path:
            return httpx.Response(200, content=b"x" * 2_000_001)
        return httpx.Response(200, content=b"x" * 8_000_001)

    with httpx.Client(transport=httpx.MockTransport(oversized)) as client:
        remote = RemoteClient("http://127.0.0.1:9001", TOKEN, client=client)
        with pytest.raises(RemoteError, match="size limit"):
            remote.recent_workbench()
        with pytest.raises(RemoteError, match="size limit"):
            remote.workbench_artifact("1" * 32, "figure-1.png")


@pytest.mark.parametrize(
    "name",
    [
        "",
        "/health",
        "../health",
        "figure-1.png/../../health",
        r"..\health.png",
        "%2e%2e/health.png",
        "nested/figure-1.png",
        "figure-0.png",
        "figure-6.png",
        "figure-01.png",
        "figure-1.jpg",
    ],
)
def test_remote_workbench_rejects_invalid_artifact_path_without_request(name):
    requests = []

    def worker(request):
        requests.append(request)
        return httpx.Response(200, content=b"unreachable")

    with httpx.Client(transport=httpx.MockTransport(worker)) as client:
        remote = RemoteClient("http://127.0.0.1:9001", TOKEN, client=client)
        with pytest.raises(ValueError, match="[Aa]rtifact"):
            remote.workbench_artifact("1" * 32, name)

    assert requests == []


@pytest.mark.parametrize(
    "name",
    [
        "../health.png",
        r"..\health.png",
        "%2e%2e%2fhealth.png",
        "nested/figure-1.png",
        "figure-6.png",
        "figure-1.jpg",
    ],
)
def test_web_workbench_rejects_invalid_artifact_path_without_worker_request(
    tmp_path, name
):
    requests = []

    def worker(request):
        requests.append(request)
        return httpx.Response(200, content=b"unreachable")

    def authorize(request: Request) -> None:
        pass

    with httpx.Client(transport=httpx.MockTransport(worker)) as http_client:
        remote = RemoteClient("http://127.0.0.1:9001", TOKEN, client=http_client)
        app = FastAPI()
        app.include_router(
            workbench_router(
                "web-token",
                authorize,
                remote_factory=lambda: remote,
            )
        )
        with TestClient(app, base_url="http://127.0.0.1") as client:
            response = client.get(
                "/api/workbench/runs/"
                + "1" * 32
                + "/artifacts/"
                + quote(name, safe=""),
            )

    assert response.status_code == 404
    assert requests == []


def write_examples(root):
    examples = root / "examples"
    examples.mkdir()
    (examples / "web_script.m").write_text("result = params;\n", encoding="utf-8")
    (examples / "web_function.m").write_text(
        "function result = web_function(params)\nresult = params;\nend\n",
        encoding="utf-8",
    )


@contextmanager
def configured_worker(root, monkeypatch):
    from flight_agent.worker import create_worker_app

    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    monkeypatch.setenv("MATLAB_WORKER_TOKEN", TOKEN)
    with serve_app(create_worker_app(root, TOKEN)) as url:
        monkeypatch.setenv("MATLAB_WORKER_URL", url)
        yield


def test_web_workbench_proxies_worker_run_without_local_execution(
    tmp_path, fake_engine, monkeypatch
):
    write_examples(tmp_path)
    with configured_worker(tmp_path, monkeypatch):
        with TestClient(create_web_app(tmp_path), base_url="http://127.0.0.1") as client:
            token = client.get("/api/workbench/bootstrap").json()["token"]
            headers = {"x-flight-token": token}
            response = client.post(
                "/api/workbench/runs",
                json=workbench_request().model_dump(mode="json"),
                headers=headers,
            )
            assert response.status_code == 202
            record = wait_job(
                client, response.json()["id"], headers, "/api/workbench/runs"
            )
            assert record["result"] == {"value": 6}
            image = client.get(
                f"/api/workbench/runs/{record['id']}/artifacts/figure-1.png",
                headers=headers,
            )
            assert image.status_code == 200
            assert image.headers["content-type"] == "image/png"
            assert not (tmp_path / "runs/workbench-web").exists()


def test_web_workbench_keeps_auth_origin_host_and_secret_guards(tmp_path, monkeypatch):
    monkeypatch.setenv("MATLAB_WORKER_URL", "http://127.0.0.1:9001")
    monkeypatch.setenv("MATLAB_WORKER_TOKEN", TOKEN)
    with TestClient(create_web_app(tmp_path), base_url="http://127.0.0.1") as client:
        bootstrap = client.get("/api/workbench/bootstrap")
        assert bootstrap.status_code == 200
        assert bootstrap.json()["worker_configured"] is True
        assert bootstrap.json()["worker_error"] is None
        assert TOKEN not in bootstrap.text
        headers = {"x-flight-token": bootstrap.json()["token"]}
        assert client.get("/api/workbench/runs").status_code == 403
        assert client.get(
            "/api/workbench/runs",
            headers={**headers, "Origin": "https://attacker.example"},
        ).status_code == 403
        assert client.get(
            "/api/workbench/bootstrap", headers={"Host": "attacker.example"}
        ).status_code == 400


def test_web_workbench_unconfigured_worker_is_explicitly_unavailable(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("MATLAB_WORKER_URL", raising=False)
    monkeypatch.delenv("MATLAB_WORKER_TOKEN", raising=False)
    with TestClient(create_web_app(tmp_path), base_url="http://127.0.0.1") as client:
        bootstrap = client.get("/api/workbench/bootstrap").json()
        assert bootstrap["worker_configured"] is False
        assert bootstrap["worker_error"]
        response = client.post(
            "/api/workbench/runs",
            json=workbench_request().model_dump(mode="json"),
            headers={"x-flight-token": bootstrap["token"]},
        )
        assert response.status_code == 503
        assert "result" not in response.json()


def test_web_workbench_maps_busy_and_missing_worker_resources(
    tmp_path, fake_engine, monkeypatch
):
    import flight_agent.verification.workbench_worker as worker_module

    started, release = Event(), Event()

    def blocked(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        raise ValueError("controlled workbench failure")

    monkeypatch.setattr(worker_module, "execute_workbench", blocked)
    with configured_worker(tmp_path, monkeypatch):
        with TestClient(create_web_app(tmp_path), base_url="http://127.0.0.1") as client:
            token = client.get("/api/workbench/bootstrap").json()["token"]
            headers = {"x-flight-token": token}
            first = workbench_request()
            try:
                assert client.post(
                    "/api/workbench/runs",
                    json=first.model_dump(mode="json"),
                    headers=headers,
                ).status_code == 202
                assert started.wait(timeout=2)
                second = first.model_copy(update={"id": "4" * 32})
                assert client.post(
                    "/api/workbench/runs",
                    json=second.model_dump(mode="json"),
                    headers=headers,
                ).status_code == 409
                assert client.get(
                    "/api/workbench/runs/" + "9" * 32, headers=headers
                ).status_code == 404
                assert client.get(
                    "/api/workbench/runs/"
                    + "9" * 32
                    + "/artifacts/figure-1.png",
                    headers=headers,
                ).status_code == 404
            finally:
                release.set()


@pytest.mark.parametrize(
    ("name", "status"),
    [
        ("web_script.m", 200),
        ("web_function.m", 200),
        ("parameters.json", 404),
        ("other.m", 404),
        ("../web_script.m", 404),
    ],
)
def test_web_workbench_serves_only_named_examples(tmp_path, name, status):
    write_examples(tmp_path)
    with TestClient(create_web_app(tmp_path), base_url="http://127.0.0.1") as client:
        token = client.get("/api/workbench/bootstrap").json()["token"]
        response = client.get(
            f"/api/workbench/examples/{name}",
            headers={"x-flight-token": token},
        )
        assert response.status_code == status


def test_web_workbench_examples_are_packaged_and_ignore_project_overrides(tmp_path):
    write_examples(tmp_path)
    with TestClient(create_web_app(tmp_path), base_url="http://127.0.0.1") as client:
        token = client.get("/api/workbench/bootstrap").json()["token"]
        response = client.get(
            "/api/workbench/examples/web_script.m",
            headers={"x-flight-token": token},
        )

    assert response.status_code == 200
    assert response.text == (ROOT / "examples" / "web_script.m").read_text(encoding="utf-8")
    assert response.text != "result = params;\n"
