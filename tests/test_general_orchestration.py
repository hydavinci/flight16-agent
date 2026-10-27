import json
from pathlib import Path

import pytest

from flight_agent.runtime.capabilities import Capability
from flight_agent.verification.planner import VerificationPlanner
from flight_agent.verification.runtime import VerificationRuntime
from tests.diagnosis_helpers import AdaptivePlanner, diagnostic_project
from tests.helpers import tool_message
from tests.test_general_runtime import MassInput, MassOutput, proposal
from tests.test_verification_runtime import (
    ControlledBackend,
    RecordingModel,
    confirm,
    explanation,
)
from tests.test_verification_runtime import (
    proposal as verification_proposal,
)
from tests.verification_helpers import project_tree


class UnusedBackend:
    def inspect(self, root):
        raise AssertionError("Knowledge and supplied-input capabilities must not inspect a project")

    def execute(self, *args, **kwargs):
        raise AssertionError("Knowledge and supplied-input capabilities must not dispatch MATLAB")


def shell(tmp_path, responses):
    model = RecordingModel(responses=responses)
    runtime = VerificationRuntime(
        tmp_path / "state", backend=UnusedBackend(), planner=VerificationPlanner(model)
    )
    return runtime, model


def register_mass(runtime, calls):
    def measure(context, inputs):
        calls.append(inputs.components)
        return MassOutput(total=sum(inputs.components), unit="kg")

    runtime.register_capability(Capability(
        name="mass_summary", version="2.3", description="Sum explicit synthetic masses.",
        input_model=MassInput, output_model=MassOutput, effect="read_only", operation=measure,
    ))


def test_auto_knowledge_answer_needs_no_directory_backend_or_task(tmp_path):
    runtime, model = shell(tmp_path, [tool_message("plan_intent", {
        "action": "answer", "text": "增益裕度描述开环增益变化到临界稳定的余量。",
    })])
    answered = runtime.submit(None, "增益裕度是什么？", scenario="auto")
    assert answered.status == "completed"
    assert answered.scenario == "auto"
    assert answered.project_root is None
    assert answered.task is None
    assert answered.pending_decision is None
    assert "增益裕度" in answered.messages[-1].content
    assert len(model.seen) == 1
    assert runtime.store.get(answered.id) == answered


def test_main_shell_runs_registered_strict_third_capability_without_d1_constraints(tmp_path):
    runtime, model = shell(tmp_path, [
        tool_message("plan_intent", {
            "action": "plan", "text": "汇总显式质量输入。",
            "steps": [{
                "workflow": "mass_summary", "request": "Sum synthetic masses 2 and 3.5 kg.",
                "inputs": {"components": [2.0, 3.5]},
            }],
        }),
        tool_message("propose_general", proposal().model_dump(mode="json")),
    ])
    calls = []
    register_mass(runtime, calls)
    planned = runtime.submit(None, "汇总合成组件质量 2 和 3.5 kg。", scenario="auto")
    assert planned.status == "waiting_for_user", planned.error
    assert planned.pending_decision.kind == "confirm_baseline"
    assert planned.task.scenario == "general"
    assert planned.task.workflow == "mass_summary"
    assert planned.task.baseline.specs["mass"].capability_version == "2.3"
    assert not planned.task.baseline.confirmed
    assert not hasattr(planned.task.baseline, "trim_point")
    assert not calls
    intent_context = json.loads(model.seen[0][-1])
    registered = next(
        item for item in intent_context["workflows"] if item["name"] == "mass_summary"
    )
    assert registered["input_schema"]["additionalProperties"] is False
    assert registered["version"] == "2.3"

    done = confirm(runtime, planned)
    assert done.status == "completed", done.error
    assert done.task.outputs == {"mass": {"total": 5.5, "unit": "kg"}}
    assert done.task.calls[0].tool_version == "2.3"
    assert calls == [[2.0, 3.5]]
    assert len(model.seen) == 2
    assert runtime.store.get(done.id).task == done.task


def test_unregistered_workflow_cannot_create_a_task_or_authorize_execution(tmp_path):
    runtime, _ = shell(tmp_path, [tool_message("plan_intent", {
        "action": "plan", "text": "Unregistered request.",
        "steps": [{"workflow": "arbitrary_shell", "request": "Run a command."}],
    })])
    blocked = runtime.submit(None, "Run an unregistered command.", scenario="auto")
    assert blocked.status == "waiting_for_user"
    assert blocked.task is None
    assert "registered" in blocked.error.lower()


def test_auto_verification_then_diagnosis_pauses_for_separate_data_and_new_approval(tmp_path):
    steps = [
        {"workflow": "d1", "request": "Verify synthetic step response."},
        {"workflow": "d2", "request": "Investigate the recorded pitch mismatch."},
    ]
    model = RecordingModel(responses=[
        tool_message("plan_intent", {
            "action": "plan", "text": "先验证，再基于独立登记数据诊断。", "steps": steps,
        }),
        tool_message("propose_verification", verification_proposal()),
        explanation(),
        tool_message("plan_intent", {
            "action": "plan", "text": "验证已交付，继续独立诊断。", "steps": steps[1:],
        }),
    ])
    planner = AdaptivePlanner()
    planner.model = model
    backend = ControlledBackend()
    runtime = VerificationRuntime(tmp_path / "state", backend=backend, planner=planner)
    root = project_tree(tmp_path / "verification")
    first_output = tmp_path / "verification-reports"
    first = runtime.submit(
        None, "先做阶跃验证，再诊断记录中的俯仰偏差。",
        str(root), output_root=str(first_output), scenario="auto",
    )
    assert first.pending_decision.kind == "confirm_baseline", first.error
    assert not backend.calls
    paused = confirm(runtime, first)
    assert paused.status == "waiting_for_user", paused.error
    assert paused.active_workflow == "d2"
    assert paused.pending_decision.kind == "clarify"
    assert paused.task.scenario == "d1"
    assert paused.task.stage == "completed"
    assert paused.project_root is None
    assert paused.output_root is None
    assert len(paused.workflow_agenda) == 1
    assert len(backend.calls) == 1
    assert not planner.seen
    observed = json.loads(model.seen[-1][-1])
    assert observed["phase"] == "observation"
    assert observed["observation"]["result"]["evaluation"] == "pass"
    with pytest.raises(ValueError, match="workflow|stage"):
        runtime.resume(paused.id)
    archived_task = paused.task.model_dump(mode="json")
    directory = runtime.store.task_directory(paused.id, paused.task.id, paused.task.output_root)
    prior_files = {
        item.path: (directory / item.path).read_bytes() for item in paused.task.artifacts
    }
    data_root = diagnostic_project(tmp_path / "diagnostic-data")
    next_plan = runtime.submit(paused.id, "使用独立登记的合成诊断数据。", str(data_root))
    assert next_plan.pending_decision.kind == "confirm_baseline", next_plan.error
    assert next_plan.task.scenario == "d2"
    assert next_plan.task.baseline.version == first.task.baseline.version + 1
    assert not next_plan.task.baseline.confirmed
    assert next_plan.task.project.root == str(data_root)
    assert next_plan.task.output_root is None
    assert next_plan.archived_tasks[0].model_dump(mode="json") == archived_task
    assert not planner.seen
    completed = confirm(runtime, next_plan)
    assert completed.status == "completed", completed.error
    assert len(completed.task.analysis_runs) == 2
    assert len(backend.calls) == 1
    assert completed.archived_tasks[0].model_dump(mode="json") == archived_task
    assert all((directory / name).read_bytes() == content for name, content in prior_files.items())
    saved_archive = runtime.store.get(completed.id).archived_tasks[0]
    assert saved_archive.model_dump(mode="json") == archived_task


@pytest.mark.parametrize("output_selection", [None, "", "   "])
def test_auto_queued_replan_preserves_output_reset_or_omission_after_reload(
    tmp_path, output_selection
):
    intent = tool_message("plan_intent", {
        "action": "plan", "text": "Verify the synthetic step response.",
        "steps": [{"workflow": "d1", "request": "Verify the step response."}],
    })
    model = RecordingModel(responses=[
        intent,
        tool_message("propose_verification", verification_proposal()),
        explanation(),
        intent,
        tool_message("propose_verification", verification_proposal(threshold=4)),
    ])
    backend = ControlledBackend()
    planner = VerificationPlanner(model)
    runtime = VerificationRuntime(tmp_path / "state", backend=backend, planner=planner)
    root = project_tree(tmp_path / "verification")
    custom_output = tmp_path / "reports"
    first = confirm(runtime, runtime.submit(
        None, "Verify step.", str(root), output_root=str(custom_output), scenario="auto",
    ))
    assert first.status == "completed", first.error
    queued = runtime.queue_message(
        first.id, "Replan with threshold 4%.", output_root=output_selection,
    )
    assert queued.status == "planning"
    assert runtime.store.get(first.id).status == "planning"
    planned = runtime.continue_submission(first.id)
    assert planned.pending_decision.kind == "confirm_baseline", planned.error
    assert planned.task.baseline.version == 2
    assert not planned.task.baseline.confirmed
    assert planned.task.result == first.task.result
    assert len(backend.calls) == 1
    if output_selection is None:
        assert planned.task.id == first.task.id
        assert planned.task.output_root == str(custom_output)
        assert planned.archived_tasks == []
    else:
        assert planned.task.id != first.task.id
        assert planned.task.output_root is None
        assert planned.archived_tasks == [first.task]
        assert runtime.store.root in Path(planned.task.result_directory).parents


def test_unknown_worker_state_blocks_mode_change_before_planning(tmp_path):
    model = RecordingModel(responses=[
        tool_message("propose_verification", verification_proposal()),
        tool_message("propose_verification", {"action": "clarify", "question": "Reconcile first."}),
    ])
    backend = ControlledBackend()
    backend.fail_after_completion = True
    runtime = VerificationRuntime(
        tmp_path / "state", backend=backend, planner=VerificationPlanner(model)
    )
    root = project_tree(tmp_path / "verification")
    failed = confirm(runtime, runtime.submit(None, "Verify step.", str(root)))
    assert failed.status == "interrupted"
    before = failed.task.model_dump(mode="json")
    blocked = runtime.submit(failed.id, "Switch to offline diagnosis.", scenario="d2")
    assert blocked.scenario == "d1"
    assert blocked.task.model_dump(mode="json") == before
    assert blocked.archived_tasks == []
    assert len(model.seen) == 2
    assert len(backend.calls) == 1
