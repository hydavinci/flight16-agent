import json
from pathlib import Path

import pytest

from tests.helpers import tool_message
from tests.test_verification_runtime import confirm, explanation, proposal, runtime_fixture
from tests.verification_helpers import verification_module


def failed_delivery(tmp_path, monkeypatch, candidate, error="Report destination is unavailable"):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    module = verification_module("runtime")
    original = module.deliver_report
    model.responses = [
        tool_message("propose_verification", proposal()),
        explanation(),
        tool_message("propose_verification", candidate),
    ]

    def fail(*args, **kwargs):
        raise OSError(error)

    monkeypatch.setattr(module, "deliver_report", fail)
    failed = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    return runtime, backend, model, root, failed, original


def test_failure_clarification_is_the_pending_question_and_preserves_resume(tmp_path, monkeypatch):
    question = "Is the report destination writable now?"
    runtime, backend, model, _, failed, _ = failed_delivery(
        tmp_path, monkeypatch, {"action": "clarify", "question": question}
    )
    saved = runtime.store.get(failed.id)
    assert saved.status == "failed"
    assert saved.pending_decision.kind == "resume"
    assert saved.pending_decision.message == question
    assert saved.messages[-1].content == question
    assert "unavailable" in saved.error
    assert saved.task.nodes[-1].status == "failed"
    again = runtime.resume(failed.id)
    assert again.status == "failed"
    assert again.pending_decision.message == question
    assert len(model.seen) == 3
    assert len(backend.calls) == 1


@pytest.mark.parametrize("changed_project", [False, True])
def test_recovery_plan_is_persisted_adopted_explicitly_and_requires_new_confirmation(
    tmp_path, monkeypatch, changed_project
):
    runtime, backend, model, root, failed, deliver = failed_delivery(
        tmp_path, monkeypatch, proposal(threshold=4)
    )
    assert failed.pending_decision.kind == "confirm_replan"
    assert failed.task.baseline.version == 1
    assert failed.task.baseline.criteria[0].threshold == 10.0
    assert failed.task.failure_proposal.criteria[0].threshold == 4.0
    assert len(backend.calls) == 1
    module = verification_module("runtime")
    restarted = module.VerificationRuntime(
        tmp_path / "state", backend=backend, planner=runtime.planner
    )
    restored = restarted.store.get(failed.id)
    assert restored.pending_decision.kind == "confirm_replan"
    with pytest.raises(ValueError, match="version"):
        restarted.replan(failed.id, 999, trusted=True)
    with pytest.raises(ValueError, match="consent"):
        restarted.replan(failed.id, 1, trusted=False)
    if changed_project:
        (root / "data/input.mat").write_bytes(b"repaired engineering data")
    pending = restarted.replan(failed.id, 1, trusted=True)
    assert pending.pending_decision.kind == "confirm_baseline"
    assert pending.task.baseline.version == 2
    assert pending.task.baseline.confirmed is False
    assert pending.task.baseline.criteria[0].threshold == 4.0
    assert len(backend.calls) == 1
    if changed_project:
        assert pending.task.id != failed.task.id
        assert pending.archived_tasks[0].id == failed.task.id
        assert pending.task.result is None
    else:
        assert pending.task.id == failed.task.id
        assert pending.task.result == failed.task.result
        assert pending.task.calls == failed.task.calls
    monkeypatch.setattr(module, "deliver_report", deliver)
    model.responses.append(explanation())
    completed = confirm(restarted, pending)
    assert completed.status == "completed", completed.error
    assert completed.task.evaluation.status == "fail"
    assert len(backend.calls) == (2 if changed_project else 1)
    assert list(Path(completed.task.result_directory).rglob("*.docx"))


def test_recovery_candidate_cannot_replace_an_unknown_worker_run(tmp_path):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    backend.fail_after_completion = True
    model.responses = [
        tool_message("propose_verification", proposal()),
        tool_message("propose_verification", proposal(threshold=4)),
        explanation(),
    ]
    failed = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    assert failed.status == "interrupted"
    assert failed.pending_decision.kind == "resume"
    assert failed.task.failure_proposal.action == "plan"
    run_id = failed.task.pending_request.id
    with pytest.raises(ValueError, match="reconcile|Reconcile|unknown"):
        runtime.replan(failed.id, 1, trusted=True)
    saved = runtime.store.get(failed.id)
    assert saved.task.pending_request.id == run_id
    assert saved.task.baseline.version == 1
    assert len(backend.calls) == 1
    done = runtime.resume(failed.id)
    assert done.status == "completed", done.error
    assert [request.id for request, _ in backend.calls] == [run_id, run_id]
    assert [resume for _, resume in backend.calls] == [False, True]


def test_failure_replan_validates_registered_tools_before_offering_a_decision(
    tmp_path, monkeypatch
):
    candidate = proposal()
    candidate["nodes"][0]["tool"] = "not_installed"
    _, backend, _, _, failed, _ = failed_delivery(tmp_path, monkeypatch, candidate)
    assert failed.status == "failed"
    assert failed.pending_decision.kind == "resume"
    assert failed.task.failure_proposal is None
    assert "not_installed" in failed.task.replan_error
    assert len(backend.calls) == 1


def test_replanner_receives_actionable_bounded_error_without_paths_or_credentials(
    tmp_path, monkeypatch
):
    error = (
        "Permission denied writing /private/reports/delivery.docx; "
        'Worker failed: "cannot open /private/other-project/trim.mat"; '
        "token=private-value Authorization: Bearer private-token " + "detail " * 1000
    )
    _, _, model, root, failed, _ = failed_delivery(
        tmp_path,
        monkeypatch,
        {"action": "clarify", "question": "Repair output permissions?"},
        error,
    )
    context = json.loads(model.seen[-1][-1])
    failure = context["failed_stage"]
    assert "Permission denied" in failure
    assert len(failure) <= 2000
    assert "/private/reports" not in failure
    assert "other-project" not in failure
    assert "trim.mat" not in failure
    assert "private-value" not in failure
    assert "private-token" not in failure
    assert str(root) not in json.dumps(context)
    assert failed.pending_decision.message == "Repair output permissions?"
