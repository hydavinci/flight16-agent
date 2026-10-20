from pathlib import Path

import pytest

from tests.helpers import tool_message
from tests.test_verification_runtime import confirm, explanation, proposal, runtime_fixture
from tests.verification_helpers import project_tree, verification_module


def test_output_root_is_resolved_before_consent_and_all_evidence_is_delivered_there(
    tmp_path, monkeypatch
):
    runtime, backend, _, root = runtime_fixture(tmp_path)
    output = tmp_path / "external results"
    execute = backend.execute

    def with_log(request, directory, on_progress, *, resume=False):
        result = execute(request, directory, on_progress, resume=resume)
        (directory / "matlab.log").write_text("Controlled execution log", encoding="utf-8")
        return result

    monkeypatch.setattr(backend, "execute", with_log)
    planned = runtime.submit(None, "Verify step.", str(root), output_root=str(output))
    directory = Path(planned.task.result_directory)
    assert planned.output_root == str(output)
    assert planned.task.output_root == str(output)
    assert directory.parent == output
    assert str(directory) in planned.pending_decision.message
    assert list(directory.iterdir()) == []
    assert not backend.calls
    completed = confirm(runtime, planned)
    assert completed.status == "completed", completed.error
    suffixes = {Path(item.path).suffix for item in completed.task.artifacts}
    assert {".docx", ".json", ".mat", ".png", ".log"}.issubset(suffixes)
    for artifact in completed.task.artifacts:
        path = verification_module("artifacts").verify_artifact(directory, artifact)
        assert output in path.parents
    restarted = verification_module("runtime").VerificationRuntime(
        tmp_path / "state", backend=backend, planner=runtime.planner
    )
    restored = restarted.store.get(completed.id)
    assert restored.task.result_directory == str(directory)
    assert confirm(restarted, restored).status == "completed"
    assert len(backend.calls) == 1


@pytest.mark.parametrize("reset_to_default", [False, True])
def test_output_change_archives_destination_and_reuses_only_verified_numerical_evidence(
    tmp_path, reset_to_default
):
    runtime, backend, _, root = runtime_fixture(tmp_path, [proposal(), proposal(threshold=4)])
    original_output = tmp_path / "original"
    first = confirm(
        runtime, runtime.submit(None, "Verify.", str(root), output_root=str(original_output))
    )
    original_directory = Path(first.task.result_directory)
    original_files = {
        item.path: (original_directory / item.path).read_bytes() for item in first.task.artifacts
    }
    target = "" if reset_to_default else str(tmp_path / "changed")
    planned = runtime.submit(first.id, "Change output and threshold to 4%.", output_root=target)
    assert planned.task.id != first.task.id
    assert planned.archived_tasks[0] == first.task
    assert planned.task.baseline.version == 2
    assert planned.task.baseline.confirmed is False
    assert planned.pending_decision.kind == "confirm_baseline"
    assert planned.task.result == first.task.result
    assert planned.task.artifacts == first.task.result.artifacts
    assert all(item.path.startswith("numerical/") for item in planned.task.artifacts)
    assert not list(Path(planned.task.result_directory).rglob("*.docx"))
    assert len(backend.calls) == 1
    second = confirm(runtime, planned)
    assert second.status == "completed", second.error
    assert second.task.evaluation.status == "fail"
    assert len(backend.calls) == 1
    assert all(
        (original_directory / name).read_bytes() == data for name, data in original_files.items()
    )
    if reset_to_default:
        assert second.task.output_root is None
        assert runtime.store.root in Path(second.task.result_directory).parents


def test_source_change_with_output_change_does_not_copy_stale_results(tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path, [proposal(), proposal()])
    first = confirm(runtime, runtime.submit(None, "Verify.", str(root)))
    (root / "data/input.mat").write_bytes(b"changed engineering data")
    planned = runtime.submit(first.id, "Verify repaired source.", output_root=str(tmp_path / "new"))
    assert planned.task.result is None
    assert planned.task.artifacts == []
    assert confirm(runtime, planned).status == "completed"
    assert len(backend.calls) == 2


def test_chat_can_change_project_without_reusing_a_previous_project(tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path, [proposal(), proposal()])
    first = confirm(runtime, runtime.submit(None, "Verify.", str(root)))
    other = project_tree(tmp_path / "other source")
    planned = runtime.submit(first.id, f'工程目录：“{other}”。验证阶跃。')
    assert planned.task.project.root == str(other)
    assert planned.archived_tasks[0].id == first.task.id
    assert planned.task.result is None
    assert confirm(runtime, planned).status == "completed"
    assert len(backend.calls) == 2


@pytest.mark.parametrize("location", ["relative", "source", "source_child", "source_parent"])
def test_output_root_rejects_relative_paths_and_source_overlap_before_model(tmp_path, location):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    output = {
        "relative": "relative/results",
        "source": str(root),
        "source_child": str(root / "results"),
        "source_parent": str(root.parent),
    }[location]
    planned = runtime.submit(None, "Verify.", str(root), output_root=output)
    assert planned.task is None
    assert planned.error
    assert not model.seen
    assert not backend.calls


@pytest.mark.parametrize("selection", [None, ""])
def test_default_output_under_source_is_rejected_before_planning(tmp_path, selection):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    runtime.store = verification_module("store").ConversationStore(root / "state")
    rejected = runtime.submit(None, "Verify.", str(root), output_root=selection)
    assert rejected.task is None
    assert "overlap" in rejected.error
    assert not list(runtime.store.root.glob("*/tasks"))
    assert not model.seen
    assert not backend.calls


def test_unsafe_default_reset_preserves_the_existing_custom_delivery(tmp_path):
    runtime, backend, model, root = runtime_fixture(tmp_path, [proposal(), proposal()])
    first = confirm(
        runtime,
        runtime.submit(None, "Verify.", str(root), output_root=str(tmp_path / "reports")),
    )
    assert first.status == "completed", first.error
    relocated = verification_module("store").ConversationStore(root / "state")
    (relocated.root / first.id).mkdir()
    relocated.save(first)
    runtime.store = relocated
    directory = Path(first.task.result_directory)
    files = {item.path: (directory / item.path).read_bytes() for item in first.task.artifacts}
    model_calls = len(model.seen)
    rejected = runtime.submit(first.id, "Restore default output.", output_root="")
    assert rejected.task == first.task
    assert rejected.archived_tasks == []
    assert rejected.status == first.status
    assert "overlap" in rejected.error
    assert not list(runtime.store.root.glob("*/tasks"))
    assert len(model.seen) == model_calls
    assert len(backend.calls) == 1
    restored = verification_module("store").ConversationStore(runtime.store.root).get(first.id)
    assert restored.task == first.task
    assert all((directory / name).read_bytes() == data for name, data in files.items())


def test_legacy_source_overlapping_output_cannot_resume(tmp_path):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    runtime.store = verification_module("store").ConversationStore(root / "state")
    saved = runtime.submit(None, "Verify.", str(root), output_root=str(tmp_path / "reports"))
    directory = runtime.store.task_directory(saved.id, saved.task.id, create=True)
    saved.output_root = None
    saved.task.output_root = None
    saved.task.result_directory = str(directory)
    saved.task.baseline = saved.task.baseline.model_copy(
        update={"confirmed": True, "confirmed_at": verification_module("store").now()}
    )
    runtime.store.save(saved)
    with pytest.raises(ValueError, match="overlap"):
        runtime.resume(saved.id)
    assert list(directory.iterdir()) == []
    assert len(model.seen) == 1
    assert not backend.calls


def test_output_symlink_and_replaced_task_directory_are_rejected(tmp_path):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(target, target_is_directory=True)
    rejected = runtime.submit(None, "Verify.", str(root), output_root=str(link / "results"))
    assert rejected.task is None
    assert rejected.error
    assert not model.seen
    planned = runtime.submit(None, "Verify.", str(root), output_root=str(target))
    directory = Path(planned.task.result_directory)
    directory.rmdir()
    directory.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink|Unsafe"):
        confirm(runtime, planned)
    assert not backend.calls


def test_output_move_does_not_archive_or_copy_tampered_numerical_evidence(tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path, [proposal(), proposal()])
    first = confirm(runtime, runtime.submit(None, "Verify.", str(root)))
    artifact = first.task.result.artifacts[0]
    (Path(first.task.result_directory) / artifact.path).write_bytes(b"tampered")
    rejected = runtime.submit(first.id, "Change output.", output_root=str(tmp_path / "new"))
    assert rejected.task.id == first.task.id
    assert rejected.archived_tasks == []
    assert rejected.error
    assert len(backend.calls) == 1


def test_legacy_records_without_output_root_retain_the_original_directory(tmp_path):
    runtime, backend, _, root = runtime_fixture(tmp_path)
    first = confirm(runtime, runtime.submit(None, "Verify.", str(root)))
    data = first.model_dump()
    data.pop("output_root", None)
    data["task"].pop("output_root", None)
    record = verification_module("contracts").Conversation.model_validate(data)
    runtime.store.save(record)
    restored = runtime.store.get(first.id)
    assert restored.output_root is None
    assert restored.task.output_root is None
    assert confirm(runtime, restored).task.result_directory == first.task.result_directory
    assert len(backend.calls) == 1


@pytest.mark.parametrize("use_chat", [False, True])
def test_output_change_cannot_replace_an_unknown_worker_run(tmp_path, use_chat):
    runtime, backend, model, root = runtime_fixture(tmp_path)
    backend.fail_after_completion = True
    model.responses = [
        tool_message("propose_verification", proposal()),
        tool_message(
            "propose_verification", {"action": "clarify", "question": "Resume the recorded run."}
        ),
        explanation(),
    ]
    original_output = tmp_path / "original"
    interrupted = confirm(
        runtime, runtime.submit(None, "Verify pitch.", str(root), output_root=str(original_output))
    )
    assert interrupted.status == "interrupted"
    pending_id = interrupted.task.pending_request.id
    changed_output = tmp_path / "new results"
    changed = (
        runtime.submit(interrupted.id, f'输出目录：“{changed_output}”。')
        if use_chat
        else runtime.submit(interrupted.id, "Change output.", output_root=str(changed_output))
    )
    assert changed.task == interrupted.task
    assert changed.output_root == str(original_output)
    assert changed.archived_tasks == []
    assert not changed_output.exists()
    assert len(backend.calls) == 1
    assert len(model.seen) == 2
    completed = runtime.resume(changed.id)
    assert completed.status == "completed", completed.error
    assert [resume for _, resume in backend.calls] == [False, True]
    assert all(request.id == pending_id for request, _ in backend.calls)
    assert completed.task.result_directory == interrupted.task.result_directory
