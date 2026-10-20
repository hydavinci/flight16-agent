import json
from pathlib import Path

import pytest

from flight_agent.runtime.evidence import load_run, receipt_name
from flight_agent.verification.contracts import PlanNode
from tests.helpers import tool_message
from tests.test_verification_runtime import confirm, explanation, proposal, runtime_fixture
from tests.verification_helpers import verification_module


def test_d1_records_immutable_branches_and_reuses_numerics_not_thresholds(tmp_path):
    runtime, backend, _, root = runtime_fixture(
        tmp_path, [proposal(), proposal(threshold=4.0)]
    )
    first = confirm(runtime, runtime.submit(None, "Verify pitch.", str(root)))
    assert first.status == "completed", first.error
    assert len(first.task.analysis_runs) == 1
    original = first.task.analysis_runs[0]
    directory = Path(first.task.result_directory)
    assert load_run(directory, original.id, original.spec) == original
    second = confirm(runtime, runtime.submit(first.id, "Use a 4% overshoot criterion."))
    assert second.status == "completed", second.error
    assert second.task.analysis_runs == [original]
    assert second.task.evaluation.status == "fail"
    assert len(backend.calls) == 1


def test_adapter_version_change_appends_same_kind_instead_of_reusing_or_overwriting(
    tmp_path, monkeypatch
):
    tools = verification_module("tools")
    runtime, backend, model, root = runtime_fixture(tmp_path, [proposal(), proposal()])
    first = confirm(runtime, runtime.submit(None, "Verify pitch.", str(root)))
    original = first.task.analysis_runs[0]
    monkeypatch.setattr(tools, "SIMULINK_ANALYSIS_VERSION", "2")
    runtime = verification_module("runtime").VerificationRuntime(
        tmp_path / "state", backend=backend, planner=runtime.planner
    )
    second = confirm(runtime, runtime.submit(first.id, "Verify with the current adapter."))
    assert second.status == "completed", second.error
    assert len(backend.calls) == 2
    assert len(second.task.analysis_runs) == 2
    assert second.task.analysis_runs[0] == original
    updated = second.task.analysis_runs[1]
    assert original.spec.inputs == updated.spec.inputs
    assert original.spec.fingerprint != updated.spec.fingerprint
    assert len(second.task.result.analyses) == 1
    for run in second.task.analysis_runs:
        assert load_run(Path(second.task.result_directory), run.id, run.spec) == run


@pytest.mark.parametrize("cached_step", [False, True])
def test_pending_run_reconciles_saved_specs_after_adapter_upgrade(
    tmp_path, monkeypatch, cached_step
):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    kinds = ("step_response", "stability_margins") if cached_step else ("step_response",)
    model.responses = (
        [tool_message("propose_verification", proposal()), explanation()] if cached_step else []
    ) + [
        tool_message("propose_verification", proposal(kinds)),
        tool_message(
            "propose_verification",
            {"action": "clarify", "question": "Reconcile the recorded worker run first."},
        ),
        explanation(),
    ]
    first = (
        confirm(runtime, runtime.submit(None, "Verify step.", str(root))) if cached_step else None
    )
    backend.fail_after_completion = True
    interrupted = confirm(
        runtime, runtime.submit(first.id if first else None, "Verify the analyses.", str(root))
    )
    assert interrupted.status == "interrupted", interrupted.error
    request = interrupted.task.pending_request
    specs = dict(interrupted.task.pending_specs)
    assert request is not None
    assert set(specs) == set(request.analyses)
    monkeypatch.setattr(verification_module("tools"), "SIMULINK_ANALYSIS_VERSION", "2")
    restarted = verification_module("runtime").VerificationRuntime(
        tmp_path / "state", backend=backend, planner=runtime.planner
    )
    completed = restarted.resume(interrupted.id)
    assert completed.status == "completed", completed.error
    assert completed.task.pending_request is None
    assert completed.task.pending_specs == {}
    assert len(backend.calls) == (3 if cached_step else 2)
    assert backend.calls[-1] == (request, True)
    assert len(completed.task.analysis_runs) == len(kinds)
    for run in completed.task.analysis_runs:
        kind = run.spec.inputs["analysis"]
        if kind in specs:
            assert run.spec == specs[kind]
        else:
            assert run == first.task.analysis_runs[0]
        assert load_run(Path(completed.task.result_directory), run.id, run.spec) == run
    assert [item.kind for item in completed.task.result.analyses] == list(kinds)


def test_pending_run_is_reconciled_before_rollback_can_reuse_older_evidence(tmp_path, monkeypatch):
    tools = verification_module("tools")
    original_version = tools.SIMULINK_ANALYSIS_VERSION
    runtime, backend, model, root = runtime_fixture(tmp_path)
    model.responses = [
        tool_message("propose_verification", proposal()),
        explanation(),
        tool_message("propose_verification", proposal()),
        tool_message(
            "propose_verification",
            {"action": "clarify", "question": "Reconcile the recorded worker run first."},
        ),
        explanation(),
    ]
    first = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    original = first.task.analysis_runs[0]
    monkeypatch.setattr(tools, "SIMULINK_ANALYSIS_VERSION", "2")
    upgraded = verification_module("runtime").VerificationRuntime(
        tmp_path / "state", backend=backend, planner=runtime.planner
    )
    backend.fail_after_completion = True
    interrupted = confirm(upgraded, upgraded.submit(first.id, "Verify with the updated adapter."))
    assert interrupted.status == "interrupted", interrupted.error
    request = interrupted.task.pending_request
    spec = interrupted.task.pending_specs["step_response"]
    monkeypatch.setattr(tools, "SIMULINK_ANALYSIS_VERSION", original_version)
    restarted = verification_module("runtime").VerificationRuntime(
        tmp_path / "state", backend=backend, planner=runtime.planner
    )
    completed = restarted.resume(interrupted.id)
    assert completed.status == "completed", completed.error
    assert completed.task.pending_request is None
    assert completed.task.pending_specs == {}
    assert len(backend.calls) == 3
    assert backend.calls[-1] == (request, True)
    assert completed.task.analysis_runs[0] == original
    assert len(completed.task.analysis_runs) == 2
    assert completed.task.analysis_runs[1].spec == spec
    assert completed.task.result.run_id == request.id
    for run in completed.task.analysis_runs:
        assert load_run(Path(completed.task.result_directory), run.id, run.spec) == run


def test_legacy_pending_run_does_not_claim_the_current_adapter_version(tmp_path, monkeypatch):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    model.responses = [
        tool_message("propose_verification", proposal()),
        tool_message(
            "propose_verification",
            {"action": "clarify", "question": "Reconcile the recorded worker run first."},
        ),
        explanation(),
    ]
    backend.fail_after_completion = True
    interrupted = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    assert interrupted.status == "interrupted", interrupted.error
    request = interrupted.task.pending_request
    interrupted.task.pending_specs = {}
    runtime.store.save(interrupted)
    monkeypatch.setattr(verification_module("tools"), "SIMULINK_ANALYSIS_VERSION", "2")
    restarted = verification_module("runtime").VerificationRuntime(
        tmp_path / "state", backend=backend, planner=runtime.planner
    )
    completed = restarted.resume(interrupted.id)
    assert completed.status == "completed", completed.error
    assert completed.task.pending_request is None
    assert backend.calls == [(request, False), (request, True)]
    assert completed.task.analysis_runs[0].spec.capability_version == "unrecorded"


def test_missing_or_mutated_receipts_block_cached_d1_delivery(tmp_path):
    runtime, backend, _, root = runtime_fixture(
        tmp_path, [proposal(), proposal(threshold=4.0)]
    )
    first = confirm(runtime, runtime.submit(None, "Verify.", str(root)))
    run = first.task.analysis_runs[0]
    path = Path(first.task.result_directory) / receipt_name(run.id)
    path.unlink()
    second = confirm(runtime, runtime.submit(first.id, "Use a 4% criterion."))
    assert second.status == "failed"
    assert "receipt" in second.error.lower()
    assert len(backend.calls) == 1
    assert not any(item.path.startswith("delivery-v2/") for item in second.task.artifacts)


def test_d1_capability_invocation_uses_shared_confirmation_guard(tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path)
    pending = runtime.submit(None, "Verify.", str(root))
    with pytest.raises(ValueError, match="confirm"):
        runtime.tools.invoke(
            PlanNode(id="analyze", tool="analyze_project"), pending, pending.task, resume=False
        )
    assert not backend.calls


def test_grouping_reuses_evaluation_but_threshold_changes_recompute_it(tmp_path, monkeypatch):
    module = verification_module("runtime")
    original = module.evaluate
    evaluations = []

    def count(baseline, result):
        evaluations.append(baseline.version)
        return original(baseline, result)

    monkeypatch.setattr(module, "evaluate", count)
    runtime, backend, _, root = runtime_fixture(
        tmp_path,
        [proposal(), proposal(grouping="separate"), proposal(threshold=4.0)],
    )
    first = confirm(runtime, runtime.submit(None, "Verify.", str(root)))
    second = confirm(runtime, runtime.submit(first.id, "Split reports only."))
    assert second.status == "completed", second.error
    assert evaluations == [1]
    assert len(second.task.evaluation_runs) == 1
    assert second.task.evaluation.baseline_version == 2
    third = confirm(runtime, runtime.submit(second.id, "Use a 4% criterion."))
    assert third.status == "completed", third.error
    assert evaluations == [1, 3]
    assert len(backend.calls) == 1
    assert len(third.task.evaluation_runs) == 2
    for run in third.task.evaluation_runs:
        assert load_run(
            Path(third.task.result_directory), run.id, run.spec, layer="evaluation"
        ) == run
    index = json.loads(
        (Path(third.task.result_directory) / "delivery-v3/artifact-index.json").read_text()
    )
    assert any(item["path"].startswith("evaluation/") for item in index["artifacts"])


def test_report_reuse_is_keyed_separately_from_execution_log_updates(tmp_path, monkeypatch):
    reports = verification_module("reports")
    runtime, _, _, root = runtime_fixture(tmp_path)
    done = confirm(runtime, runtime.submit(None, "Verify.", str(root)))
    request = runtime._report_request(done, done.task)
    directory = Path(done.task.result_directory)
    before = (directory / "delivery-v1/verification.docx").read_bytes()
    rendered = []
    original = reports._write_report

    def count(*args):
        rendered.append(args[2])
        return original(*args)

    monkeypatch.setattr(reports, "_write_report", count)
    changed_log = request.model_copy(update={"execution_record": {"status": "audit refreshed"}})
    artifacts = reports.deliver_report(changed_log, directory)
    assert rendered == []
    assert (directory / "delivery-v1/verification.docx").read_bytes() == before
    assert any(item.path.endswith("artifact-index.json") for item in artifacts)
    reports.deliver_report(request.model_copy(update={"grouping": "separate"}), directory)
    assert rendered == ["delivery-v1/step-response.docx"]
