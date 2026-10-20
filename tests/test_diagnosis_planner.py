import json

import pytest

from flight_agent.verification.planner import VerificationPlanner
from flight_agent.verification.runtime import VerificationRuntime
from tests.diagnosis_helpers import AdaptivePlanner
from tests.helpers import tool_message
from tests.test_diagnosis_contracts import diagnosis_proposal
from tests.test_diagnosis_runtime import confirm, propose, runtime_fixture
from tests.test_verification_runtime import RecordingModel


def test_d2_planner_uses_its_own_schema_and_only_redacted_metadata(tmp_path):
    runtime, _, _, root = runtime_fixture(tmp_path)
    pending = propose(runtime, root)
    task = pending.task
    task.baseline = task.baseline.model_copy(
        update={"objective": f"Read {root} api_key=never-send-this"}
    )
    model = RecordingModel(responses=[tool_message("propose_diagnosis", diagnosis_proposal())])
    planner = VerificationPlanner(model)
    proposal = planner.propose_diagnosis(
        "Inspect the registered signals, not source code.",
        pending,
        task.project,
        tools=runtime.diagnosis.tools.catalog(),
    )
    assert proposal.action == "plan"
    system, encoded = model.seen[0]
    assert "D2" in system and "D1 仅支持" not in system
    context = json.loads(encoded)
    assert "project" in context and "tools" in context
    assert str(root) not in encoded
    assert "never-send-this" not in encoded
    assert '"values"' not in encoded


@pytest.mark.parametrize(
    "objective", ["Explain the persistent bias", "Explain the transient mismatch"]
)
def test_d2_clarification_reply_keeps_the_original_objective_and_question(tmp_path, objective):
    question = "Should the comparison be limited to the first two seconds?"
    model = RecordingModel(responses=[
        tool_message("propose_diagnosis", {"action": "clarify", "question": question}),
        tool_message("propose_diagnosis", diagnosis_proposal()),
    ])
    runtime, planner, backend, root = runtime_fixture(tmp_path, VerificationPlanner(model))
    clarified = runtime.submit(
        None, f"{objective} in {root}; api_key=fixture-secret", str(root), scenario="d2"
    )
    assert clarified.task is None
    assert clarified.pending_decision.message == question
    restarted = VerificationRuntime(tmp_path / "state", backend=backend, planner=planner)
    planned = restarted.submit(clarified.id, "Yes.")
    assert planned.task is not None, planned.error
    encoded = model.seen[-1][-1]
    context = json.loads(encoded)
    assert context["current_baseline"] is None
    messages = context["recent_messages"]
    assert [item["role"] for item in messages] == ["user", "assistant", "user"]
    assert objective in messages[0]["content"]
    assert messages[1]["content"] == question
    assert messages[2]["content"] == "Yes."
    assert str(root) not in encoded
    assert "fixture-secret" not in encoded
    assert not backend.calls


def test_d2_recent_messages_and_pending_decision_are_bounded_and_redacted(tmp_path):
    runtime, _, _, root = runtime_fixture(tmp_path)
    pending = propose(runtime, root)
    original = pending.messages[0]
    pending.messages = [
        original.model_copy(update={"content": f"Discarded turn {index}"})
        for index in range(4)
    ] + [
        original.model_copy(
            update={"content": f"{root} password=fixture-secret " + "bounded " * 250}
        )
        for _ in range(6)
    ]
    model = RecordingModel(responses=[tool_message("propose_diagnosis", diagnosis_proposal())])
    VerificationPlanner(model).propose_diagnosis(
        "Yes.", pending, pending.task.project, tools=runtime.diagnosis.tools.catalog()
    )
    encoded = model.seen[0][-1]
    context = json.loads(encoded)
    assert len(context["recent_messages"]) == 6
    assert all(len(item["content"]) <= 1000 for item in context["recent_messages"])
    assert context["pending_decision"]["kind"] == "confirm_baseline"
    assert len(context["pending_decision"]["message"]) <= 1000
    assert "Discarded turn" not in encoded
    assert str(root) not in encoded
    assert pending.task.result_directory not in encoded
    assert "fixture-secret" not in encoded


def test_success_review_contains_real_run_ids_metrics_and_explicit_prior_unknowns(tmp_path):
    runtime, _, _, root = runtime_fixture(tmp_path, AdaptivePlanner())
    done = confirm(runtime, propose(runtime, root))
    review = done.task.rounds[-1].review
    model = RecordingModel(responses=[tool_message("review_diagnosis", review.model_dump())])
    planner = VerificationPlanner(model)
    returned = planner.review_diagnosis(done.task, tools=runtime.diagnosis.tools.catalog())
    assert returned == review
    context = json.loads(model.seen[0][-1])
    assert [item["run_id"] for item in context["evidence"]] == [
        run.id for run in done.task.analysis_runs
    ]
    assert context["evidence"][0]["output"]["metrics"]["mean_error"]["value"] == 1.0
    assert context["baseline"]["budget"]["max_rounds"] == 6
    assert str(root) not in model.seen[0][-1]
    assert '"values"' not in model.seen[0][-1]
