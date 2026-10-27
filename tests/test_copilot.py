import json
import os
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flight_agent.config import ModelSettings, create_model
from flight_agent.verification.contracts import PlanProposal
from flight_agent.verification.planner import VerificationPlanner
from flight_agent.web import create_web_app
from tests.test_verification_runtime import proposal, runtime_fixture


@pytest.fixture
def copilot_cli(tmp_path, monkeypatch):
    executable = tmp_path / "copilot"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "capture = Path(os.environ['PROBE_CAPTURE'])\n"
        "attempt = json.loads(capture.read_text()).get('calls', 0) if capture.exists() else 0\n"
        "capture.write_text(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd(), "
        "'calls': attempt + 1, "
        "'has_worker_secret': 'MATLAB_WORKER_TOKEN' in os.environ, "
        "'has_api_secret': 'LLM_API_KEY' in os.environ}))\n"
        "delays = os.environ.get('PROBE_DELAY_SEQUENCE')\n"
        "time.sleep(float(json.loads(delays)[attempt] if delays "
        "else os.environ.get('PROBE_DELAY', '0')))\n"
        "sequence = os.environ.get('PROBE_EVENTS_SEQUENCE')\n"
        "print(json.loads(sequence)[attempt] if sequence else os.environ['PROBE_EVENTS'], "
        "flush=True)\n"
        "time.sleep(float(os.environ.get('PROBE_EXIT_DELAY', '0')))\n"
        "sys.exit(int(os.environ.get('PROBE_EXIT', '0')))\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("LLM_PROVIDER", "copilot-cli")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "5")
    capture = tmp_path / "capture.json"
    monkeypatch.setenv("PROBE_CAPTURE", str(capture))
    return capture


def events(content, *, complete=True, tool=None):
    records = []
    if tool is not None:
        records.append({"type": "tool.execution_start", "data": {"toolName": tool}})
    records.append(
        {
            "type": "assistant.message",
            "data": {"content": content, "toolRequests": [], "phase": "final_answer"},
        }
    )
    if complete:
        records.append({"type": "result", "exitCode": 0})
    return "\n".join(json.dumps(record) for record in records)


def call_planner():
    return VerificationPlanner()._call(
        "propose_plan",
        PlanProposal,
        {"request": "Synthetic test only"},
        system_prompt="Propose a plan; never execute it.",
    )


def test_copilot_returns_validated_plan_without_api_credentials(copilot_cli, monkeypatch):
    monkeypatch.setenv(
        "PROBE_EVENTS", events('{"action":"clarify","question":"Which synthetic fixture?"}')
    )
    monkeypatch.setenv("LLM_API_KEY", "do-not-forward-api-secret")
    monkeypatch.setenv("MATLAB_WORKER_TOKEN", "do-not-forward-worker-secret")
    result = call_planner()
    assert result.action == "clarify"
    assert result.question == "Which synthetic fixture?"
    captured = json.loads(copilot_cli.read_text())
    assert captured["has_api_secret"] is False
    assert captured["has_worker_secret"] is False
    assert "--available-tools=skill" in captured["args"]
    assert "--output-format" in captured["args"]
    assert "--deny-tool=shell" in captured["args"]
    assert not any(arg.startswith("--allow") for arg in captured["args"])
    assert captured["cwd"] != str(Path.cwd())
    assert not Path(captured["cwd"]).exists()


def test_copilot_losslessly_shares_catalog_schemas_and_reduces_request_size(
    copilot_cli, monkeypatch
):
    from flight_agent.runtime.capabilities import CapabilityRegistry
    from flight_agent.verification.plugins import register_builtin_plugins
    from flight_agent.verification.resources import ResourceRegistry

    tools = CapabilityRegistry(agent_mode=True)
    register_builtin_plugins(
        tools, ResourceRegistry(), backend=None, planner=VerificationPlanner()
    )
    context = {
        "request": "Overshoot <=10%; settling time <=3 s; 5% band.",
        "catalog": tools.catalog(),
        "resources": [{"evidence": "Unmodified synthetic project evidence.\n" * 900}],
        "sources": [{"quote": "Original criterion text, including whitespace.\n"}],
        "catalog_schemas": {"schema_1": {"note": "Original context field, not the shared table"}},
        "validation_feedback": {"errors": [{
            "location": ["initializer"], "code": "source_selection_mismatch",
            "expected_selection": "setup/existing.m",
        }]},
    }
    original = deepcopy(context)
    monkeypatch.setenv("PROBE_EVENTS", events('{"action":"clarify","question":"Synthetic only"}'))
    result = VerificationPlanner()._call(
        "propose_plan", PlanProposal, context, system_prompt="Never execute the plan."
    )
    assert result.action == "clarify"
    prompt = json.loads(copilot_cli.read_text())["args"][-1]
    payload = json.loads(prompt.split("\n", 1)[1])
    schemas = payload["catalog_schemas"]
    restored = deepcopy(payload["request_context"])
    references = 0
    for capability in restored["catalog"]:
        for field in ("input_schema", "output_schema"):
            schema = capability[field]
            if set(schema) == {"catalog_schema_ref"}:
                capability[field] = schemas[schema["catalog_schema_ref"]]
                references += 1
    assert references >= 7
    assert restored == original
    assert context == original
    unpacked = {key: value for key, value in payload.items() if key != "catalog_schemas"}
    unpacked["request_context"] = original
    assert len(prompt.encode("utf-8")) < 0.7 * len(
        json.dumps(unpacked, ensure_ascii=False).encode("utf-8")
    )


def test_copilot_shares_only_identical_whole_schemas_and_preserves_local_refs(
    copilot_cli, monkeypatch
):
    original_schema = {
        "$defs": {"Value": {"type": "number", "description": "Original numeric value"}},
        "properties": {"value": {"$ref": "#/$defs/Value"}},
    }
    different_schema = deepcopy(original_schema)
    different_schema["$defs"]["Value"]["type"] = "string"
    context = {"catalog": [
        {"name": "first", "output_schema": original_schema},
        {"name": "second", "output_schema": dict(reversed(list(original_schema.items())))},
        {"name": "different", "output_schema": different_schema},
    ]}
    monkeypatch.setenv("PROBE_EVENTS", events('{"action":"clarify","question":"Synthetic only"}'))
    VerificationPlanner()._call(
        "propose_plan", PlanProposal, context, system_prompt="Never execute the plan."
    )
    payload = json.loads(json.loads(copilot_cli.read_text())["args"][-1].split("\n", 1)[1])
    catalog = payload["request_context"]["catalog"]
    first = catalog[0]["output_schema"]
    assert first == catalog[1]["output_schema"]
    assert payload["catalog_schemas"][first["catalog_schema_ref"]] == original_schema
    assert catalog[2]["output_schema"] == different_schema


@pytest.mark.parametrize(
    "context",
    [
        {},
        {"catalog": []},
        {"catalog": [{"name": "unique", "input_schema": {"type": "object"}}]},
        {"catalog": [{"input_schema": {}}, {"input_schema": {}}]},
    ],
)
def test_copilot_keeps_unhelpful_sharing_inline(copilot_cli, monkeypatch, context):
    monkeypatch.setenv("PROBE_EVENTS", events('{"action":"clarify","question":"Synthetic only"}'))
    VerificationPlanner()._call(
        "propose_plan", PlanProposal, context, system_prompt="Never execute the plan."
    )
    payload = json.loads(json.loads(copilot_cli.read_text())["args"][-1].split("\n", 1)[1])
    assert payload["request_context"] == context
    assert "catalog_schemas" not in payload


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (events("not JSON"), "JSON"),
        (events('{"action":"plan","title":"Incomplete","rationale":"test"}'), "analyses|criteria"),
        (events('{"action":"clarify","question":"test"}', complete=False), "complete"),
        (events('{"action":"clarify","question":"test"}', tool="shell"), "tool"),
        ("not JSONL", "JSON"),
    ],
)
def test_copilot_rejects_invalid_or_incomplete_output(copilot_cli, monkeypatch, output, message):
    monkeypatch.setenv("PROBE_EVENTS", output)
    with pytest.raises(ValueError, match=message):
        call_planner()


def test_copilot_failure_does_not_expose_output_or_secrets(copilot_cli, monkeypatch):
    monkeypatch.setenv("PROBE_EVENTS", "private-worker-secret")
    monkeypatch.setenv("PROBE_EXIT", "7")
    with pytest.raises(ValueError, match="exit 7") as error:
        call_planner()
    assert "private-worker-secret" not in str(error.value)


def test_copilot_timeout_is_explicit(copilot_cli, monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setenv("PROBE_DELAY", "10")
    monkeypatch.setenv("PROBE_EVENTS", "")
    with pytest.raises(ValueError, match="timed out"):
        call_planner()


def test_copilot_rejects_complete_output_when_process_does_not_finish_in_time(
    copilot_cli, monkeypatch
):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("PROBE_EXIT_DELAY", "10")
    monkeypatch.setenv("PROBE_EVENTS", events('{"action":"clarify","question":"Complete text"}'))
    with pytest.raises(ValueError, match="timed out"):
        call_planner()
    assert json.loads(copilot_cli.read_text())["calls"] == 1


@pytest.mark.parametrize("round_number", [1, 2])
def test_agent_timeout_reports_round_and_shared_total_budget(
    copilot_cli, monkeypatch, tmp_path, round_number
):
    from tests.test_agent_planner import setup

    planner, store, conversation, tools, calls, _ = setup(tmp_path, {})
    planner.model = None
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("PROBE_EVENTS", events(
        '{"action":"plan","text":"private-invalid-proposal"}'
    ))
    monkeypatch.setenv(
        "PROBE_DELAY_SEQUENCE", json.dumps([10] if round_number == 1 else [0.05, 10])
    )
    with pytest.raises(ValueError) as error:
        planner.propose(
            "Report RMSE.", conversation, catalog=tools.catalog(), resources={}, sources={}
        )
    text = str(error.value)
    assert f"第 {round_number} 轮" in text
    assert "原总预算 1s" in text and "本轮可用" in text
    assert "未自动重试" in text and "未生成新方案" in text
    assert "private-invalid-proposal" not in text
    assert json.loads(copilot_cli.read_text())["calls"] == round_number
    assert calls == [] and store.get(conversation.id).task is None


def test_bootstrap_accepts_explicit_copilot_provider_without_api_endpoint(copilot_cli, project):
    with TestClient(create_web_app(project), base_url="http://127.0.0.1") as client:
        config = client.get("/api/bootstrap").json()
    assert config["model_configured"] is True
    assert config["model_error"] is None


def test_copilot_missing_executable_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "copilot-cli")
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ValueError, match="Copilot CLI"):
        ModelSettings.from_environment()


def test_unknown_provider_does_not_fall_back_to_external_api(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "typo")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        ModelSettings.from_environment()


def test_legacy_agent_rejects_directory_only_copilot_backend(copilot_cli):
    with pytest.raises(ValueError, match="directory"):
        create_model()


def test_copilot_runtime_stops_at_confirmation_without_executing(
    copilot_cli, monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "PROBE_EVENTS", events(json.dumps(proposal(("step_response", "stability_margins"))))
    )
    runtime, backend, _, root = runtime_fixture(tmp_path)
    runtime.planner = VerificationPlanner()
    conversation = runtime.submit(None, "Verify both analyses using demo criteria.", str(root))
    assert conversation.status == "waiting_for_user", conversation.error
    assert conversation.pending_decision.kind == "confirm_baseline"
    assert not conversation.task.baseline.confirmed
    assert conversation.task.baseline.analyses == ["step_response", "stability_margins"]
    assert backend.calls == []


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf"])
def test_copilot_rejects_invalid_timeout(copilot_cli, monkeypatch, timeout):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", timeout)
    with pytest.raises(ValueError, match="LLM_TIMEOUT_SECONDS"):
        ModelSettings.from_environment()


def test_copilot_accepts_new_agent_schema_without_running_capabilities(
    copilot_cli, monkeypatch, tmp_path
):
    from flight_agent.runtime.capabilities import CapabilityRegistry
    from flight_agent.verification.agent_planner import AgentPlanner
    from flight_agent.verification.store import ConversationStore
    from tests.agent_helpers import analysis_plan_data, register_rmse
    from tests.test_agent_planner import draft

    calls = []
    tools = CapabilityRegistry(agent_mode=True)
    register_rmse(tools, calls)
    monkeypatch.setenv(
        "PROBE_EVENTS",
        events(
            json.dumps(
                {
                    "action": "plan",
                    "text": "Synthetic only.",
                    "plan": draft(analysis_plan_data()),
                }
            )
        ),
    )
    store = ConversationStore(tmp_path / "state")
    conversation = store.create()
    result = AgentPlanner().propose(
        "Report synthetic RMSE.",
        conversation,
        catalog=tools.catalog(),
        resources={},
        sources={},
    )
    assert result.plan.requirements.criteria == []
    assert calls == []
    assert store.get(conversation.id).task is None


def test_copilot_agent_rejects_mixed_clarification_with_safe_error(
    copilot_cli, monkeypatch, tmp_path
):
    from tests.agent_helpers import gap_data
    from tests.test_agent_planner import setup

    planner, store, conversation, tools, calls, _ = setup(tmp_path, {})
    planner.model = None
    monkeypatch.setenv(
        "PROBE_EVENTS",
        events(json.dumps({
            "action": "clarify",
            "text": "private-invalid-response",
            "plan": None,
            "gaps": [gap_data()],
        })),
    )
    with pytest.raises(ValueError, match="模型返回.*格式") as error:
        planner.propose(
            "Report RMSE.", conversation, catalog=tools.catalog(), resources={}, sources={}
        )
    assert "private-invalid-response" not in str(error.value)
    assert json.loads(copilot_cli.read_text())["calls"] == 2
    assert calls == []
    assert store.get(conversation.id).task is None


def test_copilot_corrects_missing_output_binding_without_executing(
    copilot_cli, monkeypatch, tmp_path
):
    from tests.test_agent_planner import draft, setup

    planner, store, conversation, tools, calls, _ = setup(tmp_path, {})
    planner.model = None
    broken = draft()
    broken["requirements"]["requested_outputs"].append({
        "id": "validity_explanation", "kind": "finding", "node_id": None, "item_id": None,
        "description": "Validity of actual metrics.",
    })
    monkeypatch.setenv("PROBE_EVENTS_SEQUENCE", json.dumps([
        events(json.dumps({"action": "plan", "text": "Unbound.", "plan": broken})),
        events(json.dumps({"action": "plan", "text": "Bound.", "plan": draft()})),
    ]))
    decision = planner.propose(
        "Report RMSE and explain validity.", conversation,
        catalog=tools.catalog(), resources={}, sources={},
    )
    assert decision.action == "plan"
    capture = json.loads(copilot_cli.read_text())
    assert capture["calls"] == 2
    assert "plan_output_producer" in capture["args"][-1]
    payload = json.loads(capture["args"][-1].split("\n", 1)[1])
    assert payload["catalog_schemas"]
    assert "--deny-tool=shell" in capture["args"]
    assert calls == []
    assert store.get(conversation.id).task is None


def test_copilot_shared_catalog_preserves_source_correction_and_confirmation(
    copilot_cli, monkeypatch, tmp_path
):
    import flight_agent.web as web
    from flight_agent.runtime.requirements import CriterionSource
    from flight_agent.verification.local_matlab import LocalMatlabBackend
    from tests.matlab_project_helpers import prepared_project
    from tests.test_local_matlab_planning import REQUEST, ControlledBackend, local_draft

    def forbidden(*args, **kwargs):
        raise AssertionError("Planning must not dispatch MATLAB")

    backend = ControlledBackend()
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    monkeypatch.setattr(LocalMatlabBackend, "from_environment", classmethod(lambda cls: backend))
    monkeypatch.setattr(LocalMatlabBackend, "_spawn", forbidden)
    runtimes = []
    original_runtime = web.EngineeringRuntime

    def capture(*args, **kwargs):
        runtime = original_runtime(*args, **kwargs)
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(web, "EngineeringRuntime", capture)
    web.create_web_app(tmp_path / "web")
    runtime = runtimes[0]
    project = prepared_project(tmp_path / "project")
    record = runtime.queue_message(None, REQUEST, str(project), location="matlab_project")
    resource = runtime.resources.inspect("matlab_project", str(project))
    source = CriterionSource(
        kind="user_message", reference=f"message:{record.id}:0", quote=REQUEST
    )
    correct = local_draft({
        "resources": [resource.model_dump(mode="json", exclude={"locator"})],
        "sources": [source.model_dump(mode="json")],
    })
    broken = deepcopy(correct)
    broken["nodes"][0]["inputs"].pop("initializer")
    monkeypatch.setenv("PROBE_EVENTS_SEQUENCE", json.dumps([
        events(json.dumps({"action": "plan", "text": "Missing.", "plan": broken})),
        events(json.dumps({"action": "plan", "text": "Corrected.", "plan": correct})),
    ]))
    result = runtime.continue_submission(record.id)
    assert result.task is not None, result.messages[-1].content
    assert result.pending_decision.kind == "confirm_baseline"
    assert not result.task.baseline.confirmed
    assert result.task.baseline.nodes[0].inputs["initializer"]["path"] == "initialize.m"
    assert result.task.baseline.requirements.criteria[0].source.quote == REQUEST
    assert result.task.calls == [] and backend.requests == []
    captured = json.loads(copilot_cli.read_text())
    assert captured["calls"] == 2
    payload = json.loads(captured["args"][-1].split("\n", 1)[1])
    assert payload["catalog_schemas"]
    assert payload["request_context"]["validation_feedback"]["errors"] == [{
        "location": ["initializer"], "code": "source_selection_mismatch",
        "expected_selection": "initialize.m",
    }]
    assert runtime.store.get(record.id).pending_decision.kind == "confirm_baseline"
