import shutil
from pathlib import Path

import pytest


@pytest.fixture
def project(tmp_path):
    return tmp_path


@pytest.fixture
def repository_root():
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def fake_engine(monkeypatch, repository_root, tmp_path):
    source = repository_root / "tests" / "fixtures" / "fake_matlab"
    directory = tmp_path / "fake_matlab"
    shutil.copytree(source, directory)
    engine = directory / "matlab" / "engine.py"
    engine.write_text(
        engine.read_text(encoding="utf-8")
        + r'''

def _workbench(self, run_dir, filename, kind, parameters_json, request_sha256, max_figures):
    from PIL import Image

    directory = Path(run_dir)
    mode = os.environ.get("TEST_ENGINE_MODE", "")
    assert filename == "analysis.m"
    assert kind in {"script", "function"}
    assert json.loads(parameters_json) == {"value": 3}
    assert max_figures == 5
    assert "LLM_API_KEY" not in os.environ
    assert "OPENAI_API_KEY" not in os.environ
    assert "LANGSMITH_API_KEY" not in os.environ
    assert "MATLAB_WORKER_TOKEN" not in os.environ
    if mode == "execution_error":
        raise MatlabExecutionError("original MATLAB solver failure")
    if mode == "workbench_block":
        time.sleep(60)
    print("workbench log")
    if mode == "workbench_log_flood":
        print("x" * 1_000_001)
    if mode == "workbench_missing_receipt":
        return
    figures = ["figure-1.png"]
    receipt = {
        "request_sha256": request_sha256,
        "result": {"value": 6},
        "matlab_version": "test-engine",
        "figures": figures,
    }
    if mode == "workbench_wrong_digest":
        receipt["request_sha256"] = "0" * 64
    elif mode == "workbench_missing_result":
        del receipt["result"]
    elif mode == "workbench_too_many_figures":
        receipt["figures"] = [f"figure-{index}.png" for index in range(1, 7)]
    if mode == "workbench_nan":
        (directory / "receipt.json").write_text(
            '{"request_sha256":"' + request_sha256
            + '","result":NaN,"matlab_version":"test-engine","figures":[]}'
        )
        return
    if mode == "workbench_inf":
        (directory / "receipt.json").write_text(
            '{"request_sha256":"' + request_sha256
            + '","result":Infinity,"matlab_version":"test-engine","figures":[]}'
        )
        return
    if mode == "workbench_null":
        receipt["result"] = None
    if mode == "workbench_oversized_result":
        receipt["result"] = "x" * 4_000_001
    if mode == "workbench_oversized_receipt":
        receipt["result"] = "x" * 3_999_900
    if mode == "workbench_receipt_symlink":
        target = directory / "unsafe-receipt.json"
        target.write_text(json.dumps(receipt))
        (directory / "receipt.json").symlink_to(target)
        return
    (directory / "receipt.json").write_text(json.dumps(receipt))
    for name in receipt["figures"]:
        path = directory / name
        Image.new("RGB", (2, 2), color="blue").save(path)
    if mode == "workbench_invalid_png":
        (directory / "figure-1.png").write_bytes(b"not PNG")
    elif mode == "workbench_large_dimensions":
        Image.new("1", (4001, 4000)).save(directory / "figure-1.png")
    elif mode == "workbench_large_png":
        (directory / "figure-1.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 2_000_000)
    elif mode == "workbench_artifact_symlink":
        path = directory / "figure-1.png"
        target = directory / "unsafe-figure.png"
        path.replace(target)
        path.symlink_to(target)


_original_feval = Engine.feval


def _feval(self, name, *args, **kwargs):
    if name == "flight_agent_execute_workbench":
        kwargs.pop("nargout", None)
        return _workbench(self, *args, **kwargs)
    return _original_feval(self, name, *args, **kwargs)


def _which(self, name, **kwargs):
    if name == "flight_agent_execute_workbench":
        return str(Path(self.paths[-1]) / (name + ".m"))
    return ""


Engine.feval = _feval
Engine.which = _which
''',
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(directory))
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    monkeypatch.delenv("TEST_ENGINE_MODE", raising=False)
