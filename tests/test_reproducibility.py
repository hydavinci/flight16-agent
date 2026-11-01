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
        assert 'uv run --no-sync python -m pytest -m "not matlab" -q' in text
        assert "uv run --no-sync python -m ruff check ." in text
        assert "uv run --no-sync python -m mypy src" in text
        assert "uv run --no-sync python -m build --no-isolation" in text
