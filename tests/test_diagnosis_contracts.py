import importlib

import pytest
from pydantic import ValidationError


def contracts():
    try:
        return importlib.import_module("flight_agent.verification.diagnosis_contracts")
    except ModuleNotFoundError as exc:
        pytest.fail(f"Diagnosis contracts are missing: {exc.name}")


def diagnosis_proposal():
    return {
        "action": "plan",
        "title": "Investigate the recorded pitch mismatch",
        "rationale": "Separate a constant bias from a transient response mismatch.",
        "hypotheses": [
            {"id": "bias", "statement": "The recorded mismatch is consistent with a bias."},
            {"id": "transient", "statement": "The mismatch is limited to the transient window."},
        ],
        "allowed_capabilities": ["window_statistics", "compare_signals"],
        "analyses": [
            {
                "capability": "compare_signals",
                "inputs": {
                    "observed_dataset": "observed",
                    "observed_signal": "pitch",
                    "reference_dataset": "reference",
                    "reference_signal": "pitch",
                    "start": 0.0,
                    "end": 2.0,
                    "alignment": "linear_overlap",
                    "reference_time_offset": 0.0,
                },
                "hypothesis_ids": ["bias", "transient"],
            }
        ],
    }


def test_diagnosis_baseline_is_not_a_verification_criterion_list():
    proposal = contracts().DiagnosisProposal.model_validate(diagnosis_proposal())
    assert proposal.action == "plan"
    assert len(proposal.hypotheses) == 2
    assert proposal.budget.max_calls <= 40
    with pytest.raises(ValidationError, match="Extra inputs"):
        contracts().DiagnosisProposal.model_validate({**diagnosis_proposal(), "criteria": []})


@pytest.mark.parametrize("fault", ["duplicate", "unknown", "capability", "empty", "nonfinite"])
def test_diagnosis_plans_require_bounded_unique_linked_analyses(fault):
    data = diagnosis_proposal()
    if fault == "duplicate":
        data["hypotheses"].append(data["hypotheses"][0])
    elif fault == "unknown":
        data["analyses"][0]["hypothesis_ids"] = ["invented"]
    elif fault == "capability":
        data["allowed_capabilities"] = ["window_statistics"]
    elif fault == "empty":
        data["analyses"] = []
    else:
        data["analyses"][0]["inputs"]["start"] = float("inf")
    with pytest.raises(ValidationError):
        contracts().DiagnosisProposal.model_validate(data)


def test_supported_and_refuted_assessments_require_actual_citations():
    schema = contracts().HypothesisAssessment
    for status in ("supported", "refuted"):
        with pytest.raises(ValidationError, match="evidence|citation"):
            schema(hypothesis_id="bias", status=status, reason="Claim without evidence.")
    unknown = schema(hypothesis_id="bias", status="unknown", reason="Missing observations.")
    assert unknown.evidence == []


def test_decisions_cannot_mix_new_execution_with_a_stop_or_hide_unknowns():
    schema = contracts().DiagnosisReview
    base = {
        "reason": "Need the steady-state window.",
        "assessments": [
            {"hypothesis_id": "bias", "status": "unknown", "reason": "Need more data."}
        ],
        "unknowns": ["Transient and steady-state evidence have not been separated."],
    }
    with pytest.raises(ValidationError):
        schema.model_validate({**base, "action": "continue", "analyses": []})
    with pytest.raises(ValidationError):
        schema.model_validate(
            {
                **base,
                "action": "stop",
                "stop_reason": "evidence_sufficient",
                "analyses": diagnosis_proposal()["analyses"],
            }
        )
    with pytest.raises(ValidationError, match="unknown"):
        schema.model_validate(
            {**base, "action": "stop", "stop_reason": "missing_input", "unknowns": []}
        )
