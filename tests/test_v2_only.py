import importlib.util
from pathlib import Path

import pytest

from flight_agent.verification import contracts

REMOVED_SYMBOLS = {
    "VerificationRuntime",
    "LegacyRuntimeAdapter",
    "VerificationTask",
    "DiagnosisTask",
    "GeneralTask",
    "LegacyTask",
    "legacy_decision",
    "to_legacy_view",
    "GeneralBaseline",
    "GeneralProposal",
    "WorkflowRegistry",
    "WorkflowRequest",
    "IntentProposal",
}


def test_production_source_has_no_legacy_runtime_symbols():
    root = Path(__file__).resolve().parents[1] / "src/flight_agent"
    assert root.is_dir()
    violations = {
        path.as_posix(): sorted(
            symbol for symbol in REMOVED_SYMBOLS if symbol in path.read_text("utf-8")
        )
        for path in root.rglob("*.py")
        if any(symbol in path.read_text("utf-8") for symbol in REMOVED_SYMBOLS)
    }
    assert violations == {}


@pytest.mark.parametrize(
    "name",
    [
        "compat",
        "runtime",
        "planner",
        "general",
        "diagnosis",
        "reports",
        "evaluation",
        "tools",
        "legacy_contracts",
        "diagnosis_reports",
    ],
)
def test_removed_legacy_modules_are_not_importable(name):
    assert importlib.util.find_spec(f"flight_agent.verification.{name}") is None


@pytest.mark.parametrize("name", ["intents", "workflows"])
def test_removed_workflow_modules_are_not_importable(name):
    assert importlib.util.find_spec(f"flight_agent.runtime.{name}") is None


def test_removed_upload_bridge_is_not_shipped():
    root = Path(__file__).resolve().parents[1] / "src/flight_agent"
    assert root.is_dir()
    assert not (root / "matlab_support/flight_agent_execute_upload.m").exists()


@pytest.mark.parametrize("guard", ["symbols", "upload"])
def test_filesystem_guards_detect_violations_from_another_cwd(tmp_path, monkeypatch, guard):
    root = Path(__file__).resolve().parents[1] / "src/flight_agent"
    monkeypatch.chdir(tmp_path)
    if guard == "symbols":
        original_read = Path.read_text

        def contaminated_read(path, *args, **kwargs):
            if path == root / "__init__.py":
                return "VerificationRuntime"
            return original_read(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", contaminated_read)
        with pytest.raises(AssertionError):
            test_production_source_has_no_legacy_runtime_symbols()
    else:
        original_exists = Path.exists
        bridge = root / "matlab_support/flight_agent_execute_upload.m"
        monkeypatch.setattr(
            Path, "exists", lambda path: path == bridge or original_exists(path)
        )
        with pytest.raises(AssertionError):
            test_removed_upload_bridge_is_not_shipped()


@pytest.mark.parametrize(
    "name",
    ["Criterion", "MetricCheck", "Evaluation", "METRICS", "PlanNode", "plan_ancestors"],
)
def test_fixed_verification_contracts_are_not_exported(name):
    assert not hasattr(contracts, name)


@pytest.mark.parametrize(
    "model,data",
    [
        (contracts.PendingDecision, {"kind": "confirm_replan", "message": "Obsolete recovery"}),
        (
            contracts.Decision,
            {"action": "confirm_replan", "baseline_version": 1, "created_at": "now"},
        ),
    ],
)
def test_saved_replanning_actions_are_rejected(model, data):
    with pytest.raises(ValueError):
        model.model_validate(data)
