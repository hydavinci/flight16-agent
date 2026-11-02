import ast
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow():
    return yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text("utf-8"),
        Loader=yaml.BaseLoader,
    )


def runs(job):
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_hosted_ci_has_matrix_quality_gates_and_separate_browser_job():
    data = workflow()
    assert set(data["on"]) == {"push", "pull_request", "workflow_dispatch"}
    assert "pull_request_target" not in data["on"]
    assert data["permissions"] == {"contents": "read"}
    jobs = data["jobs"]
    assert set(jobs) == {"software", "browser", "matlab"}
    assert jobs["software"]["strategy"]["matrix"]["python-version"] == [
        "3.11",
        "3.12",
    ]
    software = runs(jobs["software"])
    for command in (
        "uv lock --check",
        "uv sync --locked --extra dev",
        '-m "not matlab and not browser"',
        "python -m ruff check .",
        "python -m mypy src",
        "python -m build --no-isolation",
        "git diff --exit-code -- uv.lock",
    ):
        assert command in software
    browser = runs(jobs["browser"])
    assert "playwright install --with-deps chromium --only-shell" in browser
    assert '-m "browser and not matlab"' in browser
    assert jobs["browser"]["env"]["RUN_BROWSER_TESTS"] == "1"
    assert jobs["software"]["env"]["RUN_BROWSER_TESTS"] == "0"
    for name in ("software", "browser"):
        assert jobs[name]["env"]["RUN_MATLAB_TESTS"] == "0"
        cache = [
            step
            for step in jobs[name]["steps"]
            if step.get("uses", "").startswith("actions/cache@")
        ]
        assert len(cache) == 1
        assert cache[0]["with"]["path"] == "${{ env.UV_CACHE_DIR }}"
        assert "hashFiles('uv.lock')" in cache[0]["with"]["key"]
        for step in jobs[name]["steps"]:
            if step.get("uses", "").startswith("astral-sh/setup-uv@"):
                assert step["with"]["enable-cache"] == "false"
            if step.get("uses", "").startswith("actions/setup-python@"):
                assert "cache" not in step.get("with", {})


def test_matlab_requires_manual_trusted_licensed_runner_and_preserves_engine():
    data = workflow()
    assert "UV_CACHE_DIR" not in data.get("env", {})
    option = data["on"]["workflow_dispatch"]["inputs"]["run_matlab"]
    assert option["type"] == "boolean"
    assert option["default"] == "false"
    job = data["jobs"]["matlab"]
    assert "github.event_name == 'workflow_dispatch'" in job["if"]
    assert "inputs.run_matlab" in job["if"]
    assert "github.event.repository.default_branch" in job["if"]
    assert job["runs-on"] == ["self-hosted", "matlab"]
    assert job["environment"] == "matlab-acceptance"
    assert job["env"]["RUN_MATLAB_TESTS"] == "1"
    assert job["env"]["MATLAB_ALLOW_TRUSTED_PROJECTS"] == "1"
    assert job["env"]["UV_NO_CACHE"] == "1"
    assert "UV_CACHE_DIR" not in job["env"]
    names = [step.get("name", "") for step in job["steps"]]
    assert names.index("Check actual MATLAB and required licenses") < names.index(
        "Install locked tools without removing the preinstalled Engine"
    )
    command = runs(job)
    assert "uv sync --locked --extra dev --inexact" in command
    assert "import matlab.engine" in command
    assert "test_local_matlab_integration.py" in command
    assert not any(
        step.get("uses", "").startswith("actions/cache@")
        for step in job["steps"]
    )


def test_every_browser_test_module_has_marker_and_opt_in_gate():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    markers = project["tool"]["pytest"]["ini_options"]["markers"]
    assert any(value.startswith("browser:") for value in markers)
    for path in (ROOT / "tests").glob("test_*browser*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "pytestmark"
                for target in node.targets
            )
        )
        assert isinstance(assignment.value, ast.List), path.name
        marker = ast.unparse(assignment.value)
        assert "pytest.mark.browser" in marker
        assert "RUN_BROWSER_TESTS" in marker
