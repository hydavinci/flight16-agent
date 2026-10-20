import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from flight_agent.remote import RemoteError
from flight_agent.verification.backend import RemoteSimulinkBackend
from flight_agent.web import create_web_app
from tests.helpers import serve_app
from tests.test_worker import HEADERS, TOKEN, worker


def web_client(project: Path) -> TestClient:
    client = TestClient(create_web_app(project), base_url="http://127.0.0.1")
    client.headers["X-Flight-Token"] = client.get("/api/bootstrap").json()["token"]
    return client


def test_output_browse_lists_only_immediate_directories(project: Path) -> None:
    root = project / "output & #"
    root.mkdir()
    for name in ("zeta", "Alpha", "中文"):
        (root / name).mkdir()
    (root / "Alpha" / "nested").mkdir()
    (root / "report.txt").write_text("not a directory", encoding="utf-8")
    (root / "linked").symlink_to(root / "Alpha", target_is_directory=True)

    with web_client(project) as client:
        response = client.get(
            "/api/directories", params={"target": "output", "path": str(root)}
        )

    assert response.status_code == 200
    assert response.json() == {
        "path": str(root.resolve()),
        "parent": str(root.parent.resolve()),
        "directories": [
            {"name": name, "path": str(root.resolve() / name)}
            for name in ("Alpha", "zeta", "中文")
        ],
        "truncated": False,
    }
    assert (root / "report.txt").read_text(encoding="utf-8") == "not a directory"


def test_empty_output_path_opens_home(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = project / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    with web_client(project) as client:
        response = client.get("/api/directories", params={"target": "output"})

    assert response.status_code == 200
    assert response.json()["path"] == str(home.resolve())
    assert response.json()["directories"] == []


@pytest.mark.parametrize("path_kind", ["relative", "missing", "file", "nul", "too-long"])
def test_output_browse_rejects_invalid_path(project: Path, path_kind: str) -> None:
    file = project / "report.txt"
    file.write_text("not a directory", encoding="utf-8")
    paths = {
        "relative": "some/relative/directory",
        "missing": str(project / "missing"),
        "file": str(file),
        "nul": str(project) + "/\0invalid",
        "too-long": "/" + "x" * 4096,
    }

    with web_client(project) as client:
        response = client.get(
            "/api/directories", params={"target": "output", "path": paths[path_kind]}
        )

    assert response.status_code == 422
    assert response.json()["detail"]
    assert not (project / "missing").exists()


@pytest.mark.parametrize("host", ["web", "worker"])
@pytest.mark.parametrize("legacy_resolve", [False, True], ids=["native", "python311-312"])
def test_directory_browse_reports_symlink_loop(
    project: Path, monkeypatch: pytest.MonkeyPatch, host: str, legacy_resolve: bool
) -> None:
    loop = project / "loop"
    loop.symlink_to(loop, target_is_directory=True)
    params = {"path": str(loop)}
    if host == "worker":
        monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
        client = TestClient(worker().create_worker_app(project, TOKEN), headers=HEADERS)
        route = "/verification/directories"
    else:
        client = web_client(project)
        route = "/api/directories"
        params["target"] = "output"

    with client:
        if legacy_resolve:
            original = Path.resolve

            def resolve(path: Path, strict: bool = False) -> Path:
                if path == loop:
                    raise RuntimeError(f"Symlink loop from {loop}")
                return original(path, strict=strict)

            monkeypatch.setattr(Path, "resolve", resolve)
        response = client.get(route, params=params)

    assert response.status_code == 422
    assert str(loop) in response.json()["detail"]


def test_output_browse_reports_truncated_directory_list(project: Path) -> None:
    root = project / "many-folders"
    root.mkdir()
    for index in range(501):
        (root / f"folder-{index:03}").mkdir()

    with web_client(project) as client:
        response = client.get(
            "/api/directories", params={"target": "output", "path": str(root)}
        )

    assert response.status_code == 200
    listing = response.json()
    assert listing["truncated"] is True
    assert len(listing["directories"]) == 500
    assert [entry["name"] for entry in listing["directories"]] == [
        f"folder-{index:03}" for index in range(500)
    ]


@pytest.mark.parametrize("target", ["project", "output"])
def test_directory_browse_requires_web_auth(project: Path, target: str) -> None:
    with TestClient(create_web_app(project), base_url="http://127.0.0.1") as client:
        response = client.get(
            "/api/directories", params={"target": target, "path": str(project)}
        )

    assert response.status_code == 403
    assert "directories" not in response.json()


def test_directory_browse_rejects_unknown_target(project: Path) -> None:
    with web_client(project) as client:
        response = client.get(
            "/api/directories", params={"target": "elsewhere", "path": str(project)}
        )

    assert response.status_code == 422


def test_directory_browse_preserves_web_origin_protection(project: Path) -> None:
    with web_client(project) as client:
        response = client.get(
            "/api/directories",
            params={"target": "output", "path": str(project)},
            headers={"Origin": "https://elsewhere.example"},
        )

    assert response.status_code == 403
    assert "directories" not in response.json()


def test_output_browse_reports_permission_failure(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = os.scandir

    def denied(path):
        if Path(path) == project:
            raise PermissionError("Permission denied: controlled directory")
        return original(path)

    with web_client(project) as client:
        monkeypatch.setattr(os, "scandir", denied)
        response = client.get(
            "/api/directories", params={"target": "output", "path": str(project)}
        )

    assert response.status_code == 422
    assert "Permission denied" in response.json()["detail"]


def test_output_browse_root_has_no_parent(project: Path) -> None:
    with web_client(project) as client:
        response = client.get(
            "/api/directories", params={"target": "output", "path": project.anchor}
        )

    assert response.status_code == 200
    assert response.json()["parent"] is None


def test_worker_directory_browse_requires_auth_and_project_enablement(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_UPLOADS", "1")
    monkeypatch.delenv("MATLAB_ALLOW_TRUSTED_PROJECTS", raising=False)
    with TestClient(worker().create_worker_app(project, TOKEN)) as client:
        assert client.get("/verification/directories").status_code == 401
        response = client.get("/verification/directories", headers=HEADERS)

    assert response.status_code == 403
    assert "MATLAB_ALLOW_TRUSTED_PROJECTS" in response.json()["detail"]


def test_project_browse_uses_authenticated_worker(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project / "worker projects"
    root.mkdir()
    (root / "aircraft").mkdir()
    (root / "model.slx").write_bytes(b"not executed")
    monkeypatch.delenv("MATLAB_ALLOW_TRUSTED_UPLOADS", raising=False)
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    monkeypatch.setenv("MATLAB_WORKER_TOKEN", TOKEN)
    with serve_app(worker().create_worker_app(project, TOKEN)) as url:
        monkeypatch.setenv("MATLAB_WORKER_URL", url)
        with web_client(project) as client:
            response = client.get(
                "/api/directories", params={"target": "project", "path": str(root)}
            )

    assert response.status_code == 200
    assert response.json() == {
        "path": str(root),
        "parent": str(project),
        "directories": [{"name": "aircraft", "path": str(root / "aircraft")}],
        "truncated": False,
    }
    assert not list((project / "runs" / "verification-worker").glob("*/request.json"))


def test_project_browse_does_not_fall_back_to_agent_filesystem(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MATLAB_WORKER_URL", raising=False)
    monkeypatch.delenv("MATLAB_WORKER_TOKEN", raising=False)
    with web_client(project) as client:
        response = client.get(
            "/api/directories", params={"target": "project", "path": str(project)}
        )

    assert response.status_code == 503
    assert "MATLAB_WORKER_URL" in response.json()["detail"]
    assert "directories" not in response.json()


def test_project_browse_preserves_worker_paths_and_query_encoding(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = r"D:\工程 & #\aircraft"
    listing = {
        "path": path,
        "parent": r"D:\工程 & #",
        "directories": [{"name": "data", "path": path + r"\data"}],
        "truncated": False,
    }
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=listing)

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        backend = RemoteSimulinkBackend("https://worker.example", TOKEN, client=transport)
        monkeypatch.setattr(
            RemoteSimulinkBackend, "from_environment", classmethod(lambda cls: backend)
        )
        with web_client(project) as client:
            response = client.get(
                "/api/directories", params={"target": "project", "path": path}
            )
        assert not transport.is_closed

    assert response.status_code == 200
    assert response.json() == listing
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/verification/directories"
    assert dict(request.url.params) == {"path": path}
    assert request.headers["Authorization"] == HEADERS["Authorization"]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (422, {"detail": "not a directory"}),
        (200, {"path": "/untrusted"}),
        (200, {"path": "/", "parent": None, "directories": [], "truncated": "no"}),
    ],
)
def test_remote_browse_rejects_worker_errors_and_malformed_data(status: int, body: dict) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    ) as client:
        backend = RemoteSimulinkBackend("https://worker.example", TOKEN, client=client)
        with pytest.raises(RemoteError, match="Directory browsing failed"):
            backend.browse("")


def test_project_browse_surfaces_worker_connection_failure(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("worker unavailable", request=request)

    with httpx.Client(transport=httpx.MockTransport(unavailable)) as transport:
        backend = RemoteSimulinkBackend("https://worker.example", TOKEN, client=transport)
        monkeypatch.setattr(
            RemoteSimulinkBackend, "from_environment", classmethod(lambda cls: backend)
        )
        with web_client(project) as client:
            response = client.get(
                "/api/directories", params={"target": "project", "path": str(project)}
            )

    assert response.status_code == 503
    assert "worker unavailable" in response.json()["detail"]
