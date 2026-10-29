import json

from flight_agent.verification.diagnostic_data import diagnostic_capabilities
from flight_agent.verification.diagnostic_engine import DiagnosticEngine
from flight_agent.verification.diagnostic_planner import DiagnosticReviewer
from tests.diagnosis_helpers import AdaptivePlanner, EvolvingPlanner, diagnostic_state
from tests.helpers import RecordingModel, tool_message


def test_diagnostic_reviewer_returns_grounded_review(tmp_path):
    state = diagnostic_state(tmp_path)
    expected = {
        "action": "stop",
        "reason": "The registered data cannot distinguish the remaining hypothesis.",
        "assessments": [
            {
                "hypothesis_id": item.id,
                "status": "unknown",
                "reason": "No discriminating evidence is available.",
                "evidence": [],
            }
            for item in state.current_hypotheses
        ],
        "unknowns": ["Additional independent evidence is required."],
        "analyses": [],
        "stop_reason": "unsupported",
        "hypothesis_revision": None,
        "summary": None,
    }
    model = RecordingModel(
        responses=[tool_message("review_diagnosis", expected)]
    )
    reviewer = DiagnosticReviewer(model)
    review = reviewer.review_diagnosis(
        state,
        tools=diagnostic_capabilities().catalog(),
    )
    assert review.action in ("continue", "stop")
    assert not hasattr(reviewer, "propose_diagnosis")


def test_success_review_contains_real_run_ids_metrics_and_explicit_prior_unknowns(tmp_path):
    tools = diagnostic_capabilities()
    state = DiagnosticEngine(tools, AdaptivePlanner()).run(
        diagnostic_state(tmp_path), checkpoint=lambda state: None,
    )
    review = state.rounds[-1].review
    model = RecordingModel(responses=[tool_message("review_diagnosis", review.model_dump())])
    returned = DiagnosticReviewer(model).review_diagnosis(state, tools=tools.catalog())
    assert returned == review
    context = json.loads(model.seen[0][-1])
    assert [item["run_id"] for item in context["evidence"]] == [
        run.id for run in state.analysis_runs
    ]
    assert context["evidence"][0]["output"]["metrics"]["mean_error"]["value"] == 1.0
    assert context["baseline"]["budget"]["max_rounds"] == 6
    assert context["previous_assessments"] == [
        item.model_dump(mode="json") for item in state.assessments
    ]
    assert str(state.project.root) not in model.seen[0][-1]
    assert '"values"' not in model.seen[0][-1]


def test_review_context_includes_redacted_current_hypotheses_and_revision_history(tmp_path):
    tools = diagnostic_capabilities()
    task = DiagnosticEngine(tools, AdaptivePlanner()).run(
        diagnostic_state(tmp_path), checkpoint=lambda state: None,
    )
    root = task.project.root
    review = EvolvingPlanner().review_diagnosis(
        task.model_copy(update={"rounds": [], "analysis_runs": task.analysis_runs[:1]}),
        tools=tools.catalog(),
    )
    revision = review.hypothesis_revision
    revision = revision.model_copy(update={
        "reason": f"Recorded offset in {root}; api_key=fixture-secret",
        "hypotheses": [
            revision.hypotheses[0].model_copy(update={
                "statement": f"Offset in {root}; api_key=fixture-secret",
            }),
            *revision.hypotheses[1:],
        ],
    })
    review = review.model_copy(update={"hypothesis_revision": revision})
    task.rounds = [task.rounds[0].model_copy(update={"review": review})]
    task.assessments = review.assessments
    model = RecordingModel(responses=[tool_message("review_diagnosis", review.model_dump())])
    returned = DiagnosticReviewer(model).review_diagnosis(task, tools=tools.catalog())
    assert returned.summary.responsibility == "undetermined"
    encoded = model.seen[0][-1]
    context = json.loads(encoded)
    assert [item["id"] for item in context["current_hypotheses"]] == [
        "bias", "transient", "alignment",
    ]
    assert context["current_hypotheses"][0]["priority"] == "high"
    assert context["hypothesis_revisions"][0]["plan_version"] == 1
    assert context["hypothesis_revisions"][0]["revision"]["evidence"][0]["run_id"] == (
        task.analysis_runs[0].id
    )
    assert str(root) not in encoded and "fixture-secret" not in encoded
