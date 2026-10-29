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
    root = Path("src/flight_agent")
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
    assert not Path(
        "src/flight_agent/matlab_support/flight_agent_execute_upload.m"
    ).exists()


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
