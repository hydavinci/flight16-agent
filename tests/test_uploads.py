import importlib
import os

import pytest
from pydantic import ValidationError


def module(name):
    try:
        return importlib.import_module(f"flight_agent.{name}")
    except ModuleNotFoundError:
        pytest.fail(f"Upload component {name} has not been implemented")


@pytest.fixture
def upload_data():
    return {
        "filename": "calculation.m",
        "source": "result = struct('value', params.value * 2);",
        "kind": "script",
        "parameters": {"value": 3},
        "trusted": True,
        "timeout_seconds": 10,
    }


@pytest.mark.parametrize(
    "filename",
    ["../x.m", "/x.m", "a\\b.m", "x.M", "a-b.m", "1.m", "flight_agent_execute_upload.m", "for.m"],
)
def test_upload_rejects_unsafe_filename(upload_data, filename):
    upload_data["filename"] = filename
    with pytest.raises(ValidationError):
        module("upload_models").UploadRequest.model_validate(upload_data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("trusted", False),
        ("trusted", "true"),
        ("source", ""),
        ("source", "\x00"),
        ("source", "x" * 1_000_001),
        ("parameters", {"x": float("nan")}),
        ("parameters", [1, 2]),
        ("timeout_seconds", 0),
        ("timeout_seconds", 601),
    ],
    ids=[
        "no-consent",
        "string-consent",
        "empty-source",
        "nul-source",
        "large-source",
        "nonfinite",
        "array-params",
        "zero-timeout",
        "large-timeout",
    ],
)
def test_upload_rejects_invalid_contract(upload_data, field, value):
    upload_data[field] = value
    with pytest.raises(ValidationError):
        module("upload_models").UploadRequest.model_validate(upload_data)


def test_upload_requires_matching_function_entry(upload_data):
    upload_data.update(kind="function", source="function result = wrong(params)\nresult=1;\nend")
    with pytest.raises(ValidationError, match="function"):
        module("upload_models").UploadRequest.model_validate(upload_data)


@pytest.mark.parametrize("kind", ["script", "function"])
def test_uploaded_file_round_trip_through_engine(tmp_path, upload_data, fake_engine, kind):
    upload_data.update(kind=kind)
    if kind == "function":
        upload_data["source"] = "function result = calculation(params)\nresult=params;\nend"
    request = module("upload_models").UploadRequest.model_validate(upload_data)
    job_dir = tmp_path / "job"
    output = module("upload_engine").execute_upload(job_dir, request)
    assert output.result == {"value": 6}
    assert output.matlab_version == "test-engine"
    assert "upload log" in output.log
    assert output.figures == ["figure-1.png"]
    assert (job_dir / "figure-1.png").read_bytes().startswith(b"\x89PNG")
    assert (job_dir / "code" / "calculation.m").read_text() == request.source


@pytest.mark.parametrize(
    "mode,fragment",
    [
        ("execution_error", "solver"),
        ("startup_error", "license"),
        ("upload_missing", "result"),
        ("upload_nan", "finite"),
        ("upload_bad_image", "PNG"),
        ("upload_large", "limit"),
    ],
)
def test_upload_failure_is_explicit(
    tmp_path, upload_data, fake_engine, monkeypatch, mode, fragment
):
    monkeypatch.setenv("TEST_ENGINE_MODE", mode)
    request = module("upload_models").UploadRequest.model_validate(upload_data)
    with pytest.raises(module("matlab").MatlabError, match=fragment):
        module("upload_engine").execute_upload(tmp_path / "job", request)


def test_upload_timeout_stops_process(tmp_path, upload_data, fake_engine, monkeypatch):
    upload_data["timeout_seconds"] = 1
    monkeypatch.setenv("TEST_ENGINE_MODE", "upload_block")
    request = module("upload_models").UploadRequest.model_validate(upload_data)
    with pytest.raises(module("matlab").MatlabError, match="timed out"):
        module("upload_engine").execute_upload(tmp_path / "job", request)


def test_restarting_job_store_marks_incomplete_jobs(tmp_path, upload_data):
    models = module("upload_models")
    store = module("jobs").JobStore(tmp_path)
    record = store.create(models.UploadRequest.model_validate(upload_data))
    store.update(record.id, status="running")
    restarted = module("jobs").JobStore(tmp_path)
    record = restarted.get(record.id)
    assert record.status == "interrupted"
    assert record.error


def test_store_rejects_paths_and_hides_unknown_ids(tmp_path):
    store = module("jobs").JobStore(tmp_path)
    for path in ("../secrets", "a" * 32):
        with pytest.raises(KeyError):
            store.get(path)


@pytest.mark.matlab
@pytest.mark.skipif(os.environ.get("RUN_MATLAB_TESTS") != "1", reason="Live MATLAB is opt-in")
@pytest.mark.parametrize("kind", ["script", "function"])
def test_live_uploaded_matlab(tmp_path, kind):
    models = module("upload_models")
    source = "result = struct('value', params.value * 2); plot([1 2], [3 4]);"
    if kind == "function":
        source = "function result = calculation(params)\n" + source + "\nend"
    request = models.UploadRequest(
        filename="calculation.m",
        source=source,
        kind=kind,
        parameters={"value": 3},
        trusted=True,
        timeout_seconds=120,
    )
    output = module("upload_engine").execute_upload(tmp_path / "job", request)
    assert output.result == {"value": 6}
    assert output.figures == ["figure-1.png"]
    assert output.matlab_version != "test-engine"


@pytest.mark.matlab
@pytest.mark.skipif(os.environ.get("RUN_MATLAB_TESTS") != "1", reason="Live MATLAB is opt-in")
def test_live_upload_preserves_original_matlab_error(tmp_path):
    request = module("upload_models").UploadRequest(
        filename="calculation.m",
        source="disp('Before expected failure'); error('Expected upload failure');",
        kind="script",
        parameters={},
        trusted=True,
        timeout_seconds=120,
    )
    with pytest.raises(module("matlab").MatlabError, match="Expected upload failure"):
        module("upload_engine").execute_upload(tmp_path / "job", request)
    log = (tmp_path / "job/matlab.log").read_text()
    assert "Before expected failure" in log
    assert "Expected upload failure" in log
