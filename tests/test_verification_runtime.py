import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image
from pydantic import Field

from tests.helpers import ScriptedModel, tool_message
from tests.verification_helpers import (
    baseline_data,
    margin_result_data,
    project_tree,
    step_result_data,
    verification_module,
)


def proposal(kinds=("step_response",), threshold=10.0, grouping="combined"):
    criteria = baseline_data(kinds)["criteria"]
    for item in criteria:
        if item["metric"] == "overshoot":
            item["threshold"] = threshold
    return {
        "action": "plan",
        "title": "Pitch verification",
        "analyses": list(kinds),
        "criteria": criteria,
        "trim_point": 1,
        "grouping": grouping,
        "rationale": "Verify only the registered analyses and confirmed criteria.",
        "nodes": [
            {"id": "analyze", "tool": "analyze_project", "depends_on": []},
            {"id": "evaluate", "tool": "evaluate_results", "depends_on": ["analyze"]},
            {"id": "deliver", "tool": "deliver_report", "depends_on": ["evaluate"]},
        ],
    }


def explanation():
    return tool_message(
        "explain_evidence",
        {
            "text": "These results apply only to the recorded conditions.",
            "evidence": ["step_response.overshoot", "step_response.settling_time"],
        },
    )


class RecordingModel(ScriptedModel):
    seen: list = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append([message.content for message in messages])
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class ControlledBackend:
    def __init__(self):
        self.calls = []
        self.fail_after_completion = False
        self.completed = {}

    def inspect(self, root):
        return verification_module("projects").ProjectInspector().inspect(root)

    def execute(self, request, directory, on_progress, *, resume=False):
        from flight_agent.remote import RemoteError

        contracts = verification_module("contracts")
        self.calls.append((request, resume))
        if resume:
            assert request.id in self.completed
        data = step_result_data()
        data["analyses"] = []
        for kind in request.analyses:
            item = step_result_data() if kind == "step_response" else margin_result_data()
            data["analyses"].extend(item["analyses"])
        directory.mkdir(parents=True, exist_ok=True)
        names = [name for item in data["analyses"] for name in item["artifact_paths"]]
        for name in names:
            path = directory / name
            if path.suffix == ".png":
                Image.new("RGB", (32, 32)).save(path)
            else:
                path.write_bytes(b"MATLAB test fixture" + b" " * 128)
        data.update(
            run_id=request.id,
            project_fingerprint=request.project_fingerprint,
            trim_point=request.trim_point,
            artifacts=[
                verification_module("artifacts").describe_artifact(directory, name).model_dump()
                for name in names
            ],
        )
        result = contracts.AnalysisResult.model_validate(data)
        (directory / "analysis.json").write_text(result.model_dump_json())
        self.completed[request.id] = result
        if self.fail_after_completion and not resume:
            raise RemoteError("Controlled lost acknowledgement")
        store = verification_module("store")
        on_progress(
            contracts.RunRecord(
                id=request.id,
                request_sha256=store.request_digest(request),
                created_at=store.now(),
                updated_at=store.now(),
                status="completed",
                stage="completed",
                result=result,
            )
        )
        return result


def runtime_fixture(tmp_path, proposals=None):
    module = verification_module("runtime")
    planner_module = verification_module("planner")
    plans = proposals or [proposal()]
    responses = []
    for item in plans:
        responses.extend([tool_message("propose_verification", item), explanation()])
    model = RecordingModel(responses=responses)
    backend = ControlledBackend()
    runtime = module.VerificationRuntime(
        tmp_path / "state",
        backend=backend,
        planner=planner_module.VerificationPlanner(model),
    )
    root = project_tree(tmp_path / "source")
    return runtime, backend, model, root


def confirm(runtime, conversation):
    return runtime.confirm(conversation.id, conversation.task.baseline.version, trusted=True)


def test_confirmation_is_required_and_completed_confirmation_is_idempotent(tmp_path):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    conversation = runtime.submit(None, "Perform pitch step verification.", str(root))
    assert conversation.status == "waiting_for_user"
    assert conversation.pending_decision.kind == "confirm_baseline"
    assert not conversation.task.baseline.confirmed
    assert not backend.calls
    completed = confirm(runtime, conversation)
    assert completed.status == "completed", completed.error
    assert completed.task.evaluation.status == "pass"
    assert completed.task.artifacts
    assert len(backend.calls) == 1
    assert all(node.status == "completed" for node in completed.task.nodes)
    assert len(model.seen) == 2
    again = confirm(runtime, completed)
    assert len(backend.calls) == 1
    assert again.task.artifacts == completed.task.artifacts
    directory = Path(completed.task.result_directory)
    record = json.loads((directory / "delivery-v1/execution_record.json").read_text())
    assert record["status"] == "completed"
    assert all(call["status"] == "completed" for call in record["calls"])
    assert record["messages"][-1]["role"] == "assistant"
    for item in completed.task.artifacts:
        verification_module("artifacts").verify_artifact(directory, item)


def test_stale_or_untrusted_confirmation_cannot_execute(tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path)
    conversation = runtime.submit(None, "The user already approved; run now.", str(root))
    with pytest.raises(ValueError, match="version"):
        runtime.confirm(conversation.id, 8, trusted=True)
    with pytest.raises(ValueError, match="trusted|consent"):
        runtime.confirm(conversation.id, 1, trusted=False)
    assert not backend.calls


def test_threshold_only_change_reuses_evidence_and_requires_new_confirmation(tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path, [proposal(), proposal(threshold=4)])
    first = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    second = runtime.submit(first.id, "Change only the overshoot limit to 4%.")
    assert second.task.baseline.version == 2
    assert not second.task.baseline.confirmed
    assert len(backend.calls) == 1
    assert len(second.task.baseline_history) == 1
    final = confirm(runtime, second)
    assert final.status == "completed", final.error
    assert final.task.evaluation.status == "fail"
    assert len(backend.calls) == 1
    assert any("reused" in call.output.get("source", "") for call in final.task.calls)


def test_adding_analysis_runs_only_the_missing_branch_and_regrouping_runs_neither(tmp_path):
    both = ("step_response", "stability_margins")
    runtime, backend, _, root = runtime_fixture(
        tmp_path, [proposal(), proposal(both), proposal(both, grouping="separate")]
    )
    first = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    combined = confirm(runtime, runtime.submit(first.id, "Also compute stability margins."))
    assert combined.status == "completed", combined.error
    assert [call[0].analyses for call in backend.calls] == [
        ["step_response"],
        ["stability_margins"],
    ]
    separate = confirm(runtime, runtime.submit(first.id, "Split the two reports."))
    assert separate.status == "completed", separate.error
    assert len(backend.calls) == 2
    current = [item for item in separate.task.artifacts if item.path.startswith("delivery-v3/")]
    assert len([item for item in current if item.role == "report"]) == 2
    assert len(separate.task.result.analyses) == 2


def test_changed_project_invalidates_approval_before_execution(tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path)
    conversation = runtime.submit(None, "Verify step.", str(root))
    (root / "data/input.mat").write_bytes(b"changed after proposal")
    changed = confirm(runtime, conversation)
    assert changed.status == "waiting_for_user"
    assert "changed" in changed.error.lower()
    assert not changed.task.baseline.confirmed
    assert not backend.calls


def test_new_project_revision_archives_old_context_instead_of_reusing_it(tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path, [proposal(), proposal()])
    first = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    old_id = first.task.id
    (root / "data/input.mat").write_bytes(b"new engineering revision")
    revised = runtime.submit(first.id, "Verify the changed project.")
    assert revised.task.id != old_id
    assert revised.archived_tasks[0].id == old_id
    done = confirm(runtime, revised)
    assert done.status == "completed", done.error
    assert len(backend.calls) == 2


@pytest.mark.parametrize("fault", ["invented_tool", "cycle", "missing_evaluation", "trim_change"])
def test_unsafe_or_incomplete_model_plan_cannot_reach_numerical_tools(tmp_path, fault):
    data = proposal()
    if fault == "invented_tool":
        data["nodes"][0]["tool"] = "eval_matlab"
    elif fault == "cycle":
        data["nodes"][0]["depends_on"] = ["deliver"]
    elif fault == "missing_evaluation":
        data["nodes"] = data["nodes"][:1]
    else:
        data["trim_point"] = 13
    runtime, backend, _, root = runtime_fixture(tmp_path, [data])
    conversation = runtime.submit(None, "Run this proposed plan.", str(root))
    assert conversation.status == "waiting_for_user"
    assert conversation.error
    assert not backend.calls
    assert conversation.task is None


def test_missing_folder_and_clarification_end_turn_without_inference_loop(tmp_path):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    missing = runtime.submit(None, "Verify the model.")
    assert missing.pending_decision.kind == "clarify"
    assert not model.seen and not backend.calls
    model.responses = [
        tool_message(
            "propose_verification",
            {"action": "clarify", "question": "Should the two analyses share one report?"},
        )
    ]
    clarified = runtime.submit(missing.id, "Analyze this package.", str(root))
    assert clarified.status == "waiting_for_user"
    assert "one report" in clarified.pending_decision.message
    assert len(model.seen) == 1 and not backend.calls


def test_restart_and_resume_reconcile_the_same_remote_id(tmp_path):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    backend.fail_after_completion = True
    model.responses = [
        tool_message("propose_verification", proposal()),
        tool_message(
            "propose_verification",
            {"action": "clarify", "question": "Reconcile the recorded worker run first."},
        ),
        explanation(),
    ]
    interrupted = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    assert interrupted.status == "interrupted", interrupted.error
    assert interrupted.task.pending_request is not None
    run_id = interrupted.task.pending_request.id
    restarted = verification_module("runtime").VerificationRuntime(
        tmp_path / "state",
        backend=backend,
        planner=verification_module("planner").VerificationPlanner(model),
    )
    restored = restarted.store.get(interrupted.id)
    assert restored.pending_decision.kind == "resume"
    completed = restarted.resume(restored.id)
    assert completed.status == "completed", completed.error
    assert [request.id for request, _ in backend.calls] == [run_id, run_id]
    assert [resume for _, resume in backend.calls] == [False, True]


def test_explanation_failure_is_explicit_without_overriding_deterministic_delivery(tmp_path):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    model.responses = [
        tool_message("propose_verification", proposal()),
        tool_message(
            "explain_evidence",
            {"text": "A made-up source supports this.", "evidence": ["unavailable.evidence"]},
        ),
    ]
    completed = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    assert completed.status == "completed", completed.error
    assert completed.task.evaluation.status == "pass"
    assert completed.task.explanation is None
    assert completed.task.explanation_error
    assert len(backend.calls) == 1


def test_model_context_is_bounded_semantic_evidence_without_source_or_local_paths(tmp_path):
    runtime, _, model, root = runtime_fixture(tmp_path)
    (root / "initialize.m").write_text("function initialize\nsecret_source_content = 123;\nend\n")
    completed = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    assert completed.status == "completed"
    serialized = json.dumps(model.seen)
    assert "secret_source_content" not in serialized
    assert str(root) not in serialized
    assert "initialize.m" not in serialized
    assert "source_code" not in serialized
    assert len(serialized) < 30_000


def test_report_only_resume_after_service_restart_does_not_reexecute_analysis(tmp_path):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    conversation = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    conversation.status = "running"
    conversation.task.nodes[-1].status = "running"
    runtime.store.save(conversation)
    restarted = verification_module("runtime").VerificationRuntime(
        tmp_path / "state",
        backend=backend,
        planner=verification_module("planner").VerificationPlanner(model),
    )
    restored = restarted.store.get(conversation.id)
    assert restored.status == "interrupted"
    completed = restarted.resume(restored.id)
    assert completed.status == "completed", completed.error
    assert len(backend.calls) == 1


def test_worker_request_is_durable_and_status_reads_do_not_wait_for_execution(tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path)
    conversation = runtime.submit(None, "Verify step.", str(root))
    started = threading.Event()
    release = threading.Event()
    execute = backend.execute

    def delayed(request, directory, on_progress, *, resume=False):
        saved = runtime.store.get(conversation.id)
        assert saved.task.pending_request == request
        assert saved.task.calls[-1].inputs["request"]["id"] == request.id
        started.set()
        assert release.wait(5)
        return execute(request, directory, on_progress, resume=resume)

    backend.execute = delayed
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(confirm, runtime, conversation)
        try:
            assert started.wait(5)
            saved = pool.submit(runtime.store.get, conversation.id).result(timeout=1)
            assert saved.status == "running"
            with pytest.raises(verification_module("runtime").BusyError):
                runtime.submit(conversation.id, "Another request.")
        finally:
            release.set()
        assert running.result(timeout=5).status == "completed"


def test_cached_evidence_integrity_is_checked_before_threshold_reuse(tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path, [proposal(), proposal(threshold=4)])
    first = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    evidence = next(item for item in first.task.result.artifacts if item.role == "data")
    (Path(first.task.result_directory) / evidence.path).write_bytes(b"corrupt cached evidence")
    changed = runtime.submit(first.id, "Change the overshoot criterion to 4%.")
    failed = confirm(runtime, changed)
    assert failed.status == "failed"
    assert failed.error
    assert len(backend.calls) == 1


def test_unknown_worker_state_cannot_be_retried_under_a_new_id(tmp_path):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    backend.fail_after_completion = True
    model.responses = [
        tool_message("propose_verification", proposal()),
        tool_message("propose_verification", {"action": "clarify", "question": "Reconcile first."}),
    ]
    interrupted = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    with pytest.raises(ValueError, match="known failed"):
        runtime.retry(interrupted.id, 1, trusted=True)
    assert len(backend.calls) == 1


def test_known_failed_run_requires_explicit_retry_and_gets_a_new_id(tmp_path):
    from flight_agent.remote import RemoteError

    runtime, backend, model, root = runtime_fixture(tmp_path)
    execute = backend.execute
    attempted = []

    def fail_once(request, directory, on_progress, *, resume=False):
        attempted.append(request.id)
        if len(attempted) == 1:
            store = verification_module("store")
            on_progress(
                verification_module("contracts").RunRecord(
                    id=request.id,
                    request_sha256=store.request_digest(request),
                    created_at=store.now(),
                    updated_at=store.now(),
                    status="failed",
                    error="Controlled compiler failure",
                )
            )
            raise RemoteError("Controlled compiler failure")
        return execute(request, directory, on_progress, resume=resume)

    backend.execute = fail_once
    model.responses = [
        tool_message("propose_verification", proposal()),
        tool_message(
            "propose_verification", {"action": "clarify", "question": "Fix the compiler."}
        ),
        explanation(),
    ]
    failed = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    assert failed.status == "failed"
    assert failed.pending_decision.kind == "retry"
    assert len(attempted) == 1
    completed = runtime.retry(failed.id, 1, trusted=True)
    assert completed.status == "completed", completed.error
    assert len(attempted) == len(set(attempted)) == 2
    assert any(call.status == "failed" for call in completed.task.calls)


def test_changed_analysis_configuration_does_not_reuse_the_old_result(tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path, [proposal(), proposal()])
    first = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    inspect = backend.inspect

    def changed_configuration(path):
        project = inspect(path)
        data = project.model_dump()
        data["config"]["steady_state"]["maximum_relative_range"] = 0.015
        return verification_module("contracts").ProjectDescriptor.model_validate(data)

    backend.inspect = changed_configuration
    changed = runtime.submit(first.id, "Verify with the updated adapter configuration.")
    completed = confirm(runtime, changed)
    assert completed.status == "completed", completed.error
    assert completed.task.id != first.task.id
    assert len(backend.calls) == 2


def test_analysis_configuration_change_invalidates_confirmation(tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path)
    waiting = runtime.submit(None, "Verify step.", str(root))
    inspect = backend.inspect

    def changed_configuration(path):
        project = inspect(path)
        data = project.model_dump()
        data["config"]["feedback_sign"] = -1
        return verification_module("contracts").ProjectDescriptor.model_validate(data)

    backend.inspect = changed_configuration
    changed = confirm(runtime, waiting)
    assert changed.status == "waiting_for_user"
    assert not changed.task.baseline.confirmed
    assert not backend.calls


def test_followup_explanation_uses_the_question_and_never_reexecutes(tmp_path):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    model.responses = [
        tool_message("propose_verification", proposal()),
        explanation(),
        tool_message("propose_verification", {"action": "explain"}),
        tool_message(
            "explain_evidence",
            {
                "text": "The settling criterion is 3 s; the recorded value is 2 s.",
                "evidence": ["step_response.settling_time"],
            },
        ),
    ]
    first = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    question = "What was the settling time criterion?"
    answered = runtime.submit(first.id, question)
    assert answered.status == "completed", answered.error
    assert answered.messages[-1].content.startswith("The settling criterion")
    assert question in json.dumps(model.seen[-1])
    assert len(backend.calls) == 1
    assert answered.task.baseline.version == 1


def test_finalization_failure_never_records_a_successful_delivery_message(tmp_path, monkeypatch):
    runtime, _, _, root = runtime_fixture(tmp_path)

    def broken_finalization(*args, **kwargs):
        raise OSError("Controlled disk failure")

    monkeypatch.setattr(
        verification_module("runtime"), "finalize_execution_record", broken_finalization
    )
    failed = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    assert failed.status == "failed"
    assert "Controlled disk failure" in failed.error
    assert not any("报告已交付" in item.content for item in failed.messages)


@pytest.mark.parametrize("followup", ["explain", "clarify", "explanation_error"])
def test_failed_delivery_followup_preserves_recovery_after_reload(tmp_path, monkeypatch, followup):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    module = verification_module("runtime")
    deliver = module.deliver_report

    def broken_delivery(*args, **kwargs):
        raise OSError("Report destination is unavailable")

    model.responses = [
        tool_message("propose_verification", proposal()),
        explanation(),
        tool_message("propose_verification", {"action": "clarify", "question": "Repair output."}),
        tool_message(
            "propose_verification",
            {"action": "clarify", "question": "Which metric?"}
            if followup == "clarify"
            else {"action": "explain"},
        ),
        explanation()
        if followup != "explanation_error"
        else tool_message("explain_evidence", {"text": "Unsupported", "evidence": ["invented"]}),
    ]
    monkeypatch.setattr(module, "deliver_report", broken_delivery)
    failed = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    assert failed.status == "failed"
    assert failed.task.evaluation is not None
    assert not list(Path(failed.task.result_directory).rglob("*.docx"))

    monkeypatch.setattr(module, "deliver_report", deliver)
    queued = runtime.queue_message(failed.id, "Explain the settling result.")
    assert runtime.store.get(queued.id).status == "planning"
    answered = runtime.continue_submission(queued.id)
    assert answered.status == "failed"
    assert answered.error == "Report destination is unavailable"
    assert answered.pending_decision.kind == "resume"
    assert answered.task.nodes[-1].status == "failed"
    assert len(backend.calls) == 1

    restarted = module.VerificationRuntime(
        tmp_path / "state",
        backend=backend,
        planner=verification_module("planner").VerificationPlanner(model),
    )
    completed = restarted.resume(answered.id)
    assert completed.status == "completed", completed.error
    assert list(Path(completed.task.result_directory).rglob("*.docx"))
    assert len(backend.calls) == 1


def test_restart_during_a_followup_keeps_the_original_delivery_failure(tmp_path, monkeypatch):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    module = verification_module("runtime")

    def broken_delivery(*args, **kwargs):
        raise OSError("Report destination is unavailable")

    monkeypatch.setattr(module, "deliver_report", broken_delivery)
    model.responses = [
        tool_message("propose_verification", proposal()),
        explanation(),
        tool_message("propose_verification", {"action": "clarify", "question": "Repair output."}),
    ]
    failed = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    runtime.queue_message(failed.id, "Explain the settling result.")
    restarted = module.VerificationRuntime(
        tmp_path / "state",
        backend=backend,
        planner=verification_module("planner").VerificationPlanner(model),
    )
    restored = restarted.store.get(failed.id)
    assert restored.status == "failed"
    assert restored.error == "Report destination is unavailable"
    assert restored.pending_decision.kind == "resume"
    assert restored.task.nodes[-1].status == "failed"


@pytest.mark.parametrize("source_changed", [False, True])
def test_terminal_worker_failure_can_be_replanned_and_confirmed(tmp_path, source_changed):
    from flight_agent.remote import RemoteError

    runtime, backend, model, root = runtime_fixture(tmp_path)
    execute = backend.execute
    attempted = []

    def fail_once(request, directory, on_progress, *, resume=False):
        attempted.append((request.id, resume))
        if len(attempted) == 1:
            store = verification_module("store")
            on_progress(
                verification_module("contracts").RunRecord(
                    id=request.id,
                    request_sha256=store.request_digest(request),
                    created_at=store.now(),
                    updated_at=store.now(),
                    status="failed",
                    error="Compiler failure",
                )
            )
            raise RemoteError("Compiler failure")
        return execute(request, directory, on_progress, resume=resume)

    backend.execute = fail_once
    model.responses = [
        tool_message("propose_verification", proposal()),
        tool_message("propose_verification", {"action": "clarify", "question": "Repair source."}),
        tool_message("propose_verification", proposal()),
        explanation(),
    ]
    failed = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    if source_changed:
        (root / "data/input.mat").write_bytes(b"Repaired engineering input")
    revised = runtime.submit(failed.id, "The compiler problem is fixed. Propose verification.")
    assert revised.pending_decision.kind == "confirm_baseline"
    assert revised.task.id != failed.task.id
    assert revised.task.baseline.version == 2
    assert not revised.task.baseline.confirmed
    assert revised.task.pending_request is None
    assert revised.archived_tasks[0].worker_run.status == "failed"
    assert revised.archived_tasks[0].pending_request.id == attempted[0][0]
    assert len(attempted) == 1
    with pytest.raises(ValueError, match="version"):
        runtime.confirm(revised.id, 1, trusted=True)
    completed = confirm(runtime, revised)
    assert completed.status == "completed", completed.error
    assert len(attempted) == 2
    assert attempted[0][0] != attempted[1][0]
    assert attempted[1][1] is False


def test_unknown_worker_cannot_be_replaced_by_a_repaired_project_request(tmp_path):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    backend.fail_after_completion = True
    model.responses = [
        tool_message("propose_verification", proposal()),
        tool_message("propose_verification", {"action": "clarify", "question": "Reconcile first."}),
    ]
    interrupted = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    (root / "data/input.mat").write_bytes(b"Repaired engineering input")
    blocked = runtime.submit(interrupted.id, "Verify the repaired project.")
    assert blocked.status == "interrupted"
    assert blocked.task.pending_request.id == interrupted.task.pending_request.id
    assert not blocked.archived_tasks
    assert len(backend.calls) == 1
    assert len(model.seen) == 2
