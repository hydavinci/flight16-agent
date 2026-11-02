import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_lock_covers_supported_python_and_development_tools():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text("utf-8"))
    assert project["project"]["requires-python"] == ">=3.11"
    assert lock["requires-python"] == ">=3.11"
    assert project["tool"]["uv"]["required-version"] == "==0.12.6"
    packages = {item["name"] for item in lock["package"]}
    assert {
        "flight-matlab-agent", "pytest", "ruff", "mypy", "build",
        "setuptools", "wheel", "pyyaml", "playwright",
    } <= packages
    assert "matlabengine" not in packages
    assert not any(name.startswith("flight-agent-v1") for name in packages)
    for package in lock["package"]:
        for artifact in [package.get("sdist"), *package.get("wheels", [])]:
            if artifact is not None:
                assert re.fullmatch(r"sha256:[a-f0-9]{64}", artifact["hash"])


def test_documented_bootstrap_uses_existing_dev_extra():
    for name in ("README.md", "docs/usage.md"):
        text = (ROOT / name).read_text("utf-8")
        assert "uv sync --extra dev" in text
        assert "python3 -m pip install uv==0.12.6" in text
        assert "uv sync --locked --extra dev" in text
        assert (
            'uv run --no-sync python -m pytest -m "not matlab and not browser" -q'
            in text
        )
        assert "uv run --no-sync python -m ruff check ." in text
        assert "uv run --no-sync python -m mypy src" in text
        assert "uv run --no-sync python -m build --no-isolation" in text


def test_docs_describe_actual_modules_jobs_and_engine_opt_in():
    architecture = (ROOT / "docs/architecture.md").read_text("utf-8")
    for name in (
        "application.py",
        "web_routes/",
        "api.js",
        "state.js",
        "render.js",
        "task-evidence.js",
        "resize-preferences.js",
        "bootstrap.js",
        "not matlab and not browser",
        "browser and not matlab",
        "uv run --no-sync python -m build --no-isolation",
        "MATLAB_ACCEPTANCE_ENV",
        "--inexact",
        "RUN_MATLAB_TESTS=1",
    ):
        assert name in architecture
    usage = (ROOT / "docs/usage.md").read_text("utf-8")
    assert "MATLAB_ACCEPTANCE_ENV" in usage
    assert "--inexact" in usage
    assert "RUN_MATLAB_TESTS=1" in usage
    assert "真实 MATLAB 验收未运行" in usage
    readme = (ROOT / "README.md").read_text("utf-8")
    assert "not matlab and not browser" in readme
    assert "application.py" in readme
    assert "verification/routes.py" in readme
    assert "通用会话提交、确认、恢复、重试" in readme
    assert "真实 MATLAB 验收未运行" in architecture
