import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from flight_agent import directories, web
from tests.test_worker import TOKEN


def native_client(project: Path, *, enabled: bool = True) -> TestClient:
    client = TestClient(
        web.create_web_app(project, native_directory_picker=enabled),
        base_url="http://127.0.0.1",
    )
    client.headers["X-Flight-Token"] = client.get("/api/bootstrap").json()["token"]
    return client


@pytest.fixture
def local_worker(monkeypatch):
    monkeypatch.setenv("MATLAB_WORKER_URL", "http://127.0.0.1:9001")
    monkeypatch.setenv("MATLAB_WORKER_TOKEN", TOKEN)


def test_native_picker_passes_paths_as_arguments_and_preserves_names(tmp_path, monkeypatch):
    initial = tmp_path / "initial ' \" 中文\n"
    chosen = tmp_path / "chosen \r\n "
    initial.mkdir()
    chosen.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(initial, target_is_directory=True)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, f"{chosen}/\n".encode(), b"")

    monkeypatch.setattr(directories.sys, "platform", "darwin")
    monkeypatch.setattr(directories.subprocess, "run", run)
    result = directories.choose_native_directory(str(linked), title="Select a project")

    assert result == str(chosen.resolve())
    args, options = calls[0]
    assert args[:2] == ["/usr/bin/osascript", "-e"]
    assert args[-2:] == [str(initial.resolve()), "Select a project"]
    assert str(initial) not in args[2]
    assert "Select a project" not in args[2]
    assert options["timeout"] == 120
    assert options["capture_output"] is True
    assert options["check"] is False
    assert not options.get("shell")
    assert not options.get("text")


@pytest.mark.parametrize("stdout", [b"", b"\n"])
def test_native_picker_cancel_returns_none_and_empty_path_starts_at_home(
    tmp_path, monkeypatch, stdout
):
    calls = []
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(directories.sys, "platform", "darwin")

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout, b"")

    monkeypatch.setattr(directories.subprocess, "run", run)

    assert directories.choose_native_directory() is None
    assert calls[0][-2] == str(tmp_path.resolve())


@pytest.mark.parametrize("path_kind", ["relative", "missing", "file", "loop"])
def test_native_picker_rejects_invalid_initial_path_before_launch(
    tmp_path, monkeypatch, path_kind
):
    file = tmp_path / "file"
    file.write_text("untouched", encoding="utf-8")
    loop = tmp_path / "loop"
    loop.symlink_to(loop, target_is_directory=True)
    paths = {
        "relative": "relative",
        "missing": str(tmp_path / "missing"),
        "file": str(file),
        "loop": str(loop),
    }
    monkeypatch.setattr(directories.sys, "platform", "darwin")

    def unexpected(*args, **kwargs):
        pytest.fail("Invalid paths must not open a native dialog")

    monkeypatch.setattr(directories.subprocess, "run", unexpected)
    with pytest.raises((ValueError, OSError)):
        directories.choose_native_directory(paths[path_kind])

    assert file.read_text(encoding="utf-8") == "untouched"
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("output", [b"relative\n", b"/missing-native-selection\n"])
def test_native_picker_rejects_invalid_selected_path(tmp_path, monkeypatch, output):
    monkeypatch.setattr(directories.sys, "platform", "darwin")
    monkeypatch.setattr(
        directories.subprocess, "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, output, b""),
    )

    with pytest.raises((ValueError, OSError)):
        directories.choose_native_directory(str(tmp_path))


@pytest.mark.parametrize("failure", ["exit", "timeout", "launch", "platform"])
def test_native_picker_reports_platform_and_process_errors(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(directories.sys, "platform", "linux" if failure == "platform" else "darwin")

    def run(args, **kwargs):
        if failure == "platform":
            pytest.fail("Unsupported platforms must not launch macOS commands")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 120)
        if failure == "launch":
            raise OSError("controlled launch failure")
        return subprocess.CompletedProcess(args, 1, b"", b"controlled native failure")

    monkeypatch.setattr(directories.subprocess, "run", run)
    with pytest.raises(directories.NativeDirectoryError) as exc:
        directories.choose_native_directory(str(tmp_path))
    assert str(exc.value)


@pytest.mark.parametrize("target", ["project", "output"])
@pytest.mark.parametrize("cancel", [False, True])
def test_native_selection_api_fills_only_selected_path(
    project, monkeypatch, local_worker, target, cancel
):
    calls = []

    def choose(path="", *, title):
        calls.append((path, title))
        return None if cancel else str(project)

    monkeypatch.setattr(web, "choose_native_directory", choose, raising=False)
    with native_client(project) as client:
        assert client.get("/api/bootstrap").json()["native_directory_picker"] is True
        response = client.post(
            "/api/directories/select", json={"target": target, "path": str(project)}
        )

    assert response.status_code == 200
    assert response.json() == {"path": None if cancel else str(project)}
    assert calls == [(str(project), "选择工程目录" if target == "project" else "选择输出根目录")]


@pytest.mark.parametrize(
    ("case", "expected"),
    [("disabled", 409), ("token", 403), ("origin", 403), ("method", 405),
     ("target", 422), ("long-path", 422), ("extra-field", 422)],
)
def test_native_selection_rejects_requests_before_opening_dialog(
    project, monkeypatch, case, expected
):
    def unexpected(*args, **kwargs):
        pytest.fail("Rejected requests must not open a native dialog")

    monkeypatch.setattr(web, "choose_native_directory", unexpected, raising=False)
    with native_client(project, enabled=case != "disabled") as client:
        if case == "token":
            client.headers.pop("X-Flight-Token")
        if case == "origin":
            client.headers["Origin"] = "https://elsewhere.example"
        body = {"target": "unknown" if case == "target" else "output", "path": str(project)}
        if case == "long-path":
            body["path"] = "/" + "x" * 4096
        if case == "extra-field":
            body["command"] = "unexpected"
        response = (
            client.get("/api/directories/select") if case == "method"
            else client.post("/api/directories/select", json=body)
        )

    assert response.status_code == expected


def test_native_project_selection_rejects_remote_worker(project, monkeypatch, local_worker):
    monkeypatch.setenv("MATLAB_WORKER_URL", "https://remote-worker.example")

    def unexpected(*args, **kwargs):
        pytest.fail("Remote project selection must not open a local native dialog")

    monkeypatch.setattr(web, "choose_native_directory", unexpected, raising=False)
    with native_client(project) as client:
        response = client.post("/api/directories/select", json={"target": "project"})

    assert response.status_code == 409
    assert "Worker" in response.json()["detail"]


@pytest.mark.parametrize("failure", ["native", "path"])
def test_native_selection_api_reports_errors_and_releases_dialog_lock(
    project, monkeypatch, failure
):
    calls = 0

    def choose(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "native":
                raise directories.NativeDirectoryError("controlled native failure")
            raise ValueError("controlled invalid directory")
        return str(project)

    monkeypatch.setattr(web, "choose_native_directory", choose, raising=False)
    with native_client(project) as client:
        response = client.post("/api/directories/select", json={"target": "output"})
        after = client.post("/api/directories/select", json={"target": "output"})

    assert response.status_code == (503 if failure == "native" else 422)
    assert "controlled" in response.json()["detail"]
    assert after.status_code == 200


def test_native_selection_api_allows_only_one_dialog(project, monkeypatch):
    entered = Event()
    release = Event()
    calls = []

    def choose(*args, **kwargs):
        calls.append(True)
        entered.set()
        assert release.wait(5)
        return str(project)

    monkeypatch.setattr(web, "choose_native_directory", choose, raising=False)
    with native_client(project) as client, ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, "/api/directories/select", json={"target": "output"})
        try:
            assert entered.wait(3)
            response = client.post("/api/directories/select", json={"target": "output"})
            assert response.status_code == 409
            assert len(calls) == 1
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
