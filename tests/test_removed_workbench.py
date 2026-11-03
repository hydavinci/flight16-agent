from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REMOVED_PATHS = (
    "examples/web_script.m",
    "examples/web_function.m",
    "src/flight_agent/examples/web_script.m",
    "src/flight_agent/examples/web_function.m",
    "src/flight_agent/static/matlab-workbench.html",
    "src/flight_agent/static/matlab-workbench.css",
    "src/flight_agent/static/matlab-workbench.js",
    "src/flight_agent/matlab_support/flight_agent_execute_workbench.m",
    "src/flight_agent/workbench_contracts.py",
    "src/flight_agent/web_routes/workbench.py",
    "src/flight_agent/verification/workbench_store.py",
    "src/flight_agent/verification/workbench_matlab.py",
    "src/flight_agent/verification/workbench_worker.py",
    "docs/superpowers/specs/2026-11-02-matlab-single-file-workbench-design.md",
    "docs/superpowers/plans/2026-11-02-matlab-single-file-workbench.md",
)


def test_workbench_source_and_resources_are_removed():
    for relative in REMOVED_PATHS:
        assert not (ROOT / relative).exists(), relative


def test_user_documentation_has_no_workbench_surface():
    user_docs = [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]
    for path in user_docs:
        relative = path.relative_to(ROOT)
        text = path.read_text("utf-8").lower()
        assert "workbench" not in text, relative
        assert "/workbench" not in text, relative
        assert "matlab-workbench" not in text, relative
        assert "web_script.m" not in text, relative
        assert "web_function.m" not in text, relative
        assert "单文件工作台" not in text, relative


def test_package_data_has_no_workbench_examples():
    text = (ROOT / "pyproject.toml").read_text("utf-8")
    assert "examples/*.m" not in text
