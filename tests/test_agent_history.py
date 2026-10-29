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


def test_existing_v2_envelope_normalizes_without_rewriting_persisted_history(tmp_path):
    store = ConversationStore(tmp_path / "state")
    record = store.create()
    record.task = agent_task(store.directory(record.id))
    record.archived_tasks = [record.task.model_copy(deep=True)]
    expected = record.task.model_dump(mode="json")
    store.save(record)
    path = store.directory(record.id) / "conversation.json"
    data = json.loads(path.read_text())
    data.pop("schema_version", None)
    data.update(scenario="auto", goal="", active_workflow=None, workflow_agenda=[])
    path.write_text(json.dumps(data))
    before = path.read_bytes()

    reloaded = ConversationStore(store.root).get(record.id)

    assert reloaded.schema_version == "2.0"
    assert isinstance(reloaded.task, contracts.AgentTask)
    assert reloaded.task.schema_version == "2.0"
    assert reloaded.task.model_dump(mode="json") == expected
    assert reloaded.archived_tasks[0].model_dump(mode="json") == expected
    assert path.read_bytes() == before
    store.save(reloaded)
    persisted = json.loads(path.read_text())
    assert persisted["schema_version"] == "2.0"
    assert not {"scenario", "goal", "active_workflow", "workflow_agenda"} & persisted.keys()


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
    runtime = importlib.import_module("flight_agent.verification.agent_runtime")
    assert runtime.has_unresolved_call(saved.task)
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
    runtime = importlib.import_module("flight_agent.verification.agent_runtime")
    assert runtime.has_unresolved_call(task) is unresolved


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


def test_existing_authenticated_history_api_can_read_v2_and_download_checked_artifacts(
    project, tmp_path
):
    from fastapi.testclient import TestClient

    from flight_agent.runtime.evidence import describe_evidence
    from flight_agent.runtime.observations import OutputArtifact
    from tests.agent_helpers import make_agent_web
    from tests.helpers import headers

    app, runtime, _, _ = make_agent_web(tmp_path, [])
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
        app,
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
