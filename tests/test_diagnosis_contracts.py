import importlib

import pytest
from pydantic import ValidationError

from flight_agent.runtime.contracts import AnalysisRun, AnalysisSpec, fingerprint


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


def recorded_evidence(*, valid=True):
    output = {
        "valid": valid,
        "metrics": {"mean_error": {"value": 1.0, "unit": "deg"}},
        "observations": ["The recorded mean error is 1 deg."],
        "unknowns": [],
        "data_fingerprints": {},
    }
    return AnalysisRun(
        id="a" * 32,
        spec=AnalysisSpec(
            capability="compare_signals", capability_version="1",
            design={
                "controller": {"id": "controller", "version": "1", "sha256": "b" * 64},
                "plant": {"id": "plant", "version": "1", "sha256": "c" * 64},
            },
            inputs={},
        ),
        output=output, output_sha256=fingerprint(output),
        started_at="2026-10-20T00:00:00Z", finished_at="2026-10-20T00:00:01Z",
    )


def structured_review():
    return {
        "action": "stop",
        "stop_reason": "evidence_sufficient",
        "reason": "The recorded mismatch persists; its physical cause is not established.",
        "assessments": [{
            "hypothesis_id": "bias", "status": "supported",
            "reason": "The recorded mean error is nonzero.",
            "evidence": [{"run_id": "a" * 32, "metric": "mean_error"}],
        }],
        "summary": {
            "problem": "Recorded pitch differs from the reference.",
            "localization": "Pitch comparison in the recorded time window.",
            "responsibility": "undetermined",
            "reason": "A recorded bias does not distinguish model and control causes.",
            "hypothesis_ids": ["bias"],
            "evidence": [{"run_id": "a" * 32, "metric": "mean_error"}],
            "recommendations": ["Check sensor scaling and time alignment before new tests."],
        },
    }


def test_hypothesis_priority_defaults_for_old_records_and_rejects_unknown_levels():
    schema = contracts().Hypothesis
    assert schema(id="bias", statement="Recorded bias").priority == "medium"
    assert schema(id="bias", statement="Recorded bias", priority="high").priority == "high"
    with pytest.raises(ValidationError):
        schema(id="bias", statement="Recorded bias", priority="urgent")


def test_legacy_review_without_summary_or_revision_remains_readable():
    data = structured_review()
    del data["summary"]
    review = contracts().DiagnosisReview.model_validate(data)
    assert review.summary is None
    assert review.hypothesis_revision is None


@pytest.mark.parametrize("missing", ["evidence", "hypothesis_ids"])
def test_responsibility_attribution_requires_evidence_and_hypotheses(missing):
    data = structured_review()["summary"]
    data["responsibility"] = "control"
    data[missing] = []
    with pytest.raises(ValidationError, match="evidence|hypothes"):
        contracts().DiagnosisSummary.model_validate(data)


@pytest.mark.parametrize("fault", ["unknown_hypothesis", "unsupported", "unlinked_evidence"])
def test_responsibility_attribution_cannot_bypass_supported_assessments(fault):
    schema = contracts()
    data = structured_review()
    data["summary"]["responsibility"] = "model"
    if fault == "unknown_hypothesis":
        data["summary"]["hypothesis_ids"] = ["invented"]
    elif fault == "unsupported":
        data["assessments"][0]["status"] = "unknown"
        data["unknowns"] = ["The physical cause is unresolved."]
        data["stop_reason"] = "missing_input"
    else:
        data["summary"]["evidence"][0]["metric"] = None
    review = schema.DiagnosisReview.model_validate(data)
    hypotheses = [schema.Hypothesis(id="bias", statement="Recorded bias")]
    with pytest.raises(ValueError, match="hypothes|supported|evidence"):
        schema.validate_review(hypotheses, review, [recorded_evidence()])


def test_cited_revision_preserves_old_hypotheses_and_assesses_the_new_snapshot():
    schema = contracts()
    hypotheses = [schema.Hypothesis(id="bias", statement="Recorded bias")]
    data = structured_review()
    data["hypothesis_revision"] = {
        "hypotheses": [
            {"id": "bias", "statement": "A persistent recorded bias.", "priority": "high"},
            {"id": "alignment", "statement": "Alignment may explain the mismatch.",
             "priority": "low"},
        ],
        "reason": "The mean error motivates checking alignment separately.",
        "evidence": [{"run_id": "a" * 32, "metric": "mean_error"}],
    }
    data["assessments"].append({
        "hypothesis_id": "alignment", "status": "unknown",
        "reason": "No alignment evidence is available.",
    })
    data["unknowns"] = ["Alignment has not been established."]
    data["stop_reason"] = "missing_input"
    review = schema.DiagnosisReview.model_validate(data)
    current = schema.validate_review(hypotheses, review, [recorded_evidence()])
    assert [(item.id, item.priority) for item in current] == [
        ("bias", "high"), ("alignment", "low"),
    ]
    assert hypotheses[0].statement == "Recorded bias"
    assert hypotheses[0].priority == "medium"


@pytest.mark.parametrize("fault", ["drop", "noop", "unknown_run", "unknown_metric", "invalid"])
def test_revision_rejects_history_loss_no_change_or_unavailable_evidence(fault):
    schema = contracts()
    hypotheses = [schema.Hypothesis(id="bias", statement="Recorded bias")]
    data = structured_review()
    revision = {
        "hypotheses": [{"id": "bias", "statement": "Recorded bias", "priority": "high"}],
        "reason": "Prioritize the recorded bias.",
        "evidence": [{"run_id": "a" * 32, "metric": "mean_error"}],
    }
    if fault == "drop":
        revision["hypotheses"][0]["id"] = "replacement"
    elif fault == "noop":
        revision["hypotheses"][0]["priority"] = "medium"
    elif fault == "unknown_run":
        revision["evidence"][0]["run_id"] = "f" * 32
    elif fault == "unknown_metric":
        revision["evidence"][0]["metric"] = "invented"
    data["hypothesis_revision"] = revision
    review = schema.DiagnosisReview.model_validate(data)
    with pytest.raises(ValueError, match="hypothes|change|evidence"):
        schema.validate_review(hypotheses, review, [recorded_evidence(valid=fault != "invalid")])


@pytest.mark.parametrize("fault", ["duplicate_hypothesis", "duplicate_citation", "no_evidence"])
def test_revision_requires_unique_hypotheses_and_auditable_evidence(fault):
    revision = {
        "hypotheses": [{"id": "bias", "statement": "Recorded bias", "priority": "high"}],
        "reason": "Prioritize the recorded bias.",
        "evidence": [{"run_id": "a" * 32, "metric": "mean_error"}],
    }
    if fault == "duplicate_hypothesis":
        revision["hypotheses"].append(revision["hypotheses"][0])
    elif fault == "duplicate_citation":
        revision["evidence"].append(revision["evidence"][0])
    else:
        revision["evidence"] = []
    with pytest.raises(ValidationError):
        contracts().HypothesisRevision.model_validate(revision)


@pytest.mark.parametrize("fault", ["unknown_run", "unknown_metric", "invalid"])
def test_summary_never_cites_unavailable_or_invalid_evidence(fault):
    schema = contracts()
    data = structured_review()
    if fault == "unknown_run":
        data["summary"]["evidence"][0]["run_id"] = "f" * 32
    elif fault == "unknown_metric":
        data["summary"]["evidence"][0]["metric"] = "invented"
    review = schema.DiagnosisReview.model_validate(data)
    with pytest.raises(ValueError, match="evidence"):
        schema.validate_review(
            [schema.Hypothesis(id="bias", statement="Recorded bias")],
            review, [recorded_evidence(valid=fault != "invalid")],
        )
