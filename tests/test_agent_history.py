import importlib
import json

import pytest

from flight_agent.runtime.agent_contracts import AgentBaseline
from flight_agent.runtime.capabilities import CapabilityRegistry
from flight_agent.verification import contracts
from flight_agent.verification.store import ConversationStore, now
from tests.agent_helpers import analysis_plan_data, register_rmse


def agent_task(directory, **changes):
    registry = CapabilityRegistry(agent_mode=True)
    register_rmse(registry, [])
    data = analysis_plan_data()
    baseline = AgentBaseline(
        **data,
        version=1,
        capability_snapshots={"offline_rmse": registry.snapshot("offline_rmse")},
    )
    return contracts.AgentTask(
        id="a" * 32,
        baseline=baseline,
        nodes=data["nodes"],
        result_directory=str(directory),
        **changes,
    )


def test_v2_and_all_legacy_task_kinds_round_trip_without_rewriting_old_json(tmp_path):
    from tests.test_diagnosis_runtime import runtime_fixture as diagnosis_fixture
    from tests.test_general_runtime import proposal as general_proposal
    from tests.test_general_runtime import setup
    from tests.test_verification_runtime import proposal, runtime_fixture

    verification, _, _, root = runtime_fixture(tmp_path / "v1", [proposal()])
    old_verification = verification.submit(None, "Verify.", str(root)).task
    diagnosis, _, _, root = diagnosis_fixture(tmp_path / "diagnosis")
    old_diagnosis = diagnosis.submit(None, "Diagnose.", str(root), scenario="d2").task
    general, _, conversation, _ = setup(tmp_path / "general")
    old_general = general.propose(conversation, general_proposal(), workflow="mass_summary").task
    originals = [old_verification, old_diagnosis, old_general]
    expected = [task.model_dump(mode="json") for task in originals]
    store = ConversationStore(tmp_path / "mixed")
    record = store.create()
    record.task = agent_task(store.directory(record.id))
    record.archived_tasks = originals
    store.save(record)
    reloaded = ConversationStore(store.root).get(record.id)
    assert isinstance(reloaded.task, contracts.AgentTask)
    assert reloaded.task.schema_version == "2.0"
    assert [task.model_dump(mode="json") for task in reloaded.archived_tasks] == expected
    assert all("schema_version" not in task for task in expected)
    compat = importlib.import_module("flight_agent.verification.compat")
    assert not compat.is_legacy_task(reloaded.task)
    assert all(compat.is_legacy_task(task) for task in reloaded.archived_tasks)


def test_restart_preserves_v2_confirmation_call_identity_inputs_and_unknown_state(tmp_path):
    store = ConversationStore(tmp_path / "state")
    record = store.create()
    baseline_task = agent_task(store.directory(record.id))
    data = baseline_task.baseline.model_dump(mode="json")
    data.update(confirmed=True, confirmed_at=now())
    baseline_task.baseline = AgentBaseline.model_validate(data)
    baseline_task.nodes[0].status = "running"
    baseline_task.calls.append(
        contracts.ToolCall(
            id="1" * 32,
            node_id="compute",
            tool="offline_rmse",
            tool_version="1",
            baseline_version=1,
            status="running",
            started_at=now(),
            inputs={"observed": [0, 2], "reference": [0, 0]},
        )
    )
    record.task = baseline_task
    record.status = "running"
    store.save(record)
    restarted = ConversationStore(store.root)
    saved = restarted.get(record.id)
    assert saved.status == "interrupted"
    assert saved.pending_decision.kind == "resume"
    assert saved.task.baseline == baseline_task.baseline
    assert saved.task.calls[0].id == "1" * 32
    assert saved.task.calls[0].status == "interrupted"
    assert saved.task.calls[0].inputs == baseline_task.calls[0].inputs
    assert saved.task.nodes[0].status == "interrupted"
    compat = importlib.import_module("flight_agent.verification.compat")
    assert compat.has_unresolved_call(saved.task)
    assert ConversationStore(store.root).get(record.id) == saved


def test_v2_node_retains_bindings_and_mutable_execution_status_without_changing_baseline(tmp_path):
    task = agent_task(tmp_path)
    before = task.baseline.model_dump(mode="json")
    task.nodes[0].status = "completed"
    task.nodes[0].error = None
    assert task.baseline.model_dump(mode="json") == before
    with pytest.raises(ValueError):
        task.nodes[0].status = "unknown"


@pytest.mark.parametrize(
    "status,unresolved",
    [("running", True), ("interrupted", True), ("failed", False), ("completed", False)],
)
def test_unresolved_detection_never_confuses_failure_with_unknown_execution(
    tmp_path, status, unresolved
):
    task = agent_task(tmp_path)
    task.calls.append(
        contracts.ToolCall(
            id="1" * 32,
            node_id="compute",
            tool="offline_rmse",
            tool_version="1",
            baseline_version=1,
            status=status,
            started_at=now(),
        )
    )
    compat = importlib.import_module("flight_agent.verification.compat")
    assert compat.has_unresolved_call(task) is unresolved


def test_v2_checkpoint_references_cannot_traverse_outside_task(tmp_path):
    with pytest.raises(ValueError, match="relative|traversal"):
        agent_task(tmp_path, checkpoints={"1" * 32: "../outside"})


def test_oversized_v2_state_cannot_replace_last_readable_conversation(tmp_path):
    store = ConversationStore(tmp_path / "state")
    record = store.create()
    record.task = agent_task(tmp_path)
    store.save(record)
    original = store.get(record.id)
    record.messages.append(
        contracts.ConversationMessage(role="user", content="x" * 8_000_000, created_at=now())
    )
    with pytest.raises(ValueError, match="8 MB|limit"):
        store.save(record)
    assert store.get(record.id) == original
    raw = json.loads((store.directory(record.id) / "conversation.json").read_text())
    assert raw["task"]["schema_version"] == "2.0"


def test_legacy_entry_points_explicitly_refuse_v2_execution_and_planning(tmp_path):
    from flight_agent.verification.planner import VerificationPlanner
    from flight_agent.verification.runtime import VerificationRuntime
    from tests.helpers import ScriptedModel

    runtime = VerificationRuntime(tmp_path / "state")
    record = runtime.store.create()
    directory = runtime.store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    runtime.store.save(record)
    with pytest.raises(ValueError, match="generic|v2"):
        runtime.confirm(record.id, 1, trusted=True)
    with pytest.raises(ValueError, match="generic|v2"):
        runtime._task_workflow(record.task)
    planner = VerificationPlanner(model=ScriptedModel(responses=[]))
    with pytest.raises(ValueError, match="generic|v2"):
        planner.plan_intent("Explain.", record, workflows=[])


def test_existing_authenticated_history_api_can_read_v2_and_download_checked_artifacts(
    project, tmp_path
):
    from fastapi.testclient import TestClient

    from flight_agent.runtime.evidence import describe_evidence
    from flight_agent.runtime.observations import OutputArtifact
    from flight_agent.verification.runtime import VerificationRuntime
    from tests.test_web import headers, web_module

    runtime = VerificationRuntime(tmp_path / "state")
    record = runtime.store.create()
    directory = runtime.store.task_directory(record.id, "a" * 32, create=True)
    record.task = agent_task(directory)
    (directory / "metric.json").write_text('{"rmse":1}')
    record.task.artifacts.append(
        OutputArtifact(
            **describe_evidence(directory, "metric.json").model_dump(),
            id="rmse-record",
            node_id="compute",
        )
    )
    runtime.store.save(record)
    with TestClient(
        web_module().create_web_app(project, verification_runtime=runtime),
        base_url="http://127.0.0.1",
    ) as client:
        auth = headers(client)
        assert client.get("/api/conversations", headers=auth).json()[0]["title"] == "Synthetic RMSE"
        endpoint = f"/api/conversations/{record.id}/tasks/{record.task.id}/artifacts/metric.json"
        assert client.get(endpoint).status_code == 403
        response = client.get(endpoint, headers=auth)
        assert response.status_code == 200
        assert response.json() == {"rmse": 1}
        (directory / "metric.json").write_text('{"rmse":2}')
        assert client.get(endpoint, headers=auth).status_code == 409


@pytest.mark.parametrize(
    "worker_status,same_id,unresolved",
    [
        ("failed", True, False),
        ("interrupted", True, False),
        ("failed", False, True),
        ("interrupted", False, True),
        ("running", True, True),
        ("completed", True, True),
    ],
)
def test_legacy_worker_terminal_failure_is_not_confused_with_unknown_call(
    tmp_path, worker_status, same_id, unresolved
):
    from flight_agent.verification.compat import has_unresolved_call
    from flight_agent.verification.store import request_digest
    from tests.test_verification_runtime import proposal, runtime_fixture

    runtime, _, _, root = runtime_fixture(tmp_path, [proposal()])
    task = runtime.submit(None, "Verify.", str(root)).task
    request = contracts.AnalysisRunRequest(
        id="1" * 32,
        project_root=str(root),
        project_fingerprint=task.project.fingerprint,
        trim_point=1,
        analyses=["step_response"],
        trusted=True,
    )
    task.pending_request = request
    task.worker_run = contracts.RunRecord(
        id=request.id if same_id else "2" * 32,
        status=worker_status,
        request_sha256=request_digest(request),
        created_at=now(),
        updated_at=now(),
    )
    assert has_unresolved_call(task) is unresolved
