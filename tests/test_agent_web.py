import threading

import pytest
from fastapi.testclient import TestClient

from flight_agent.runtime.agent_contracts import AgentPlan
from flight_agent.runtime.capabilities import CapabilityRegistry
from flight_agent.verification.agent_runtime import AgentExecutor
from flight_agent.verification.store import ConversationStore
from tests.agent_helpers import (
    draft_data,
    gap_data,
    make_agent_web,
    wait_for_conversation,
)

API = "/api/conversations"


def auth(client):
    return {"x-flight-token": client.get("/api/bootstrap").json()["token"]}


def post_message(client, headers, message="Analyze synthetic RMSE.", **extra):
    response = client.post(API, headers=headers, json={"message": message, **extra})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def pending(client, identifier, headers):
    return wait_for_conversation(
        client,
        identifier,
        headers,
        lambda item: (item.get("pending_decision") or {}).get("kind") == "confirm_baseline",
    )


def test_new_request_needs_no_scenario_directory_or_worker(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("MATLAB_WORKER_URL", raising=False)
    app, _, calls, _ = make_agent_web(
        tmp_path,
        [
            {"action": "answer", "text": "知识说明，不是工程计算结果。"},
        ],
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/bootstrap").json()["model_configured"] is True
        headers = auth(client)
        identifier = post_message(client, headers, "Explain RMSE.")
        record = wait_for_conversation(
            client,
            identifier,
            headers,
            lambda item: item["status"] == "completed",
        )
        assert record["task"] is None
        assert record["messages"][-1]["content"] == "知识说明，不是工程计算结果。"
        assert not calls


def test_missing_standard_clarifies_without_creating_or_executing_a_plan(tmp_path):
    app, _, calls, _ = make_agent_web(
        tmp_path,
        [
            {"action": "clarify", "text": "请提供合格阈值和单位。"},
        ],
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = auth(client)
        identifier = post_message(client, headers, "Verify RMSE.")
        record = wait_for_conversation(
            client,
            identifier,
            headers,
            lambda item: item["status"] == "waiting_for_user",
        )
        assert record["task"] is None
        assert record["pending_decision"]["kind"] == "clarify"
        assert not calls


def test_round_budget_is_rejected_before_confirmation_and_replanned_with_consent(tmp_path):
    from copy import deepcopy

    from tests.agent_helpers import delivery_draft

    draft = delivery_draft()
    draft["nodes"].insert(1, {**draft["nodes"][0], "id": "second-analysis"})
    draft["nodes"][-1]["depends_on"].append("second-analysis")
    draft["budget"].update(max_rounds=3, max_calls=3)
    insufficient = deepcopy(draft)
    insufficient["budget"]["max_rounds"] = 2
    app, _, calls, _ = make_agent_web(tmp_path, [
        {"action": "plan", "text": "Insufficient budget.", "plan": insufficient},
        {"action": "plan", "text": "Still insufficient.", "plan": insufficient},
        {"action": "plan", "text": "Confirm all three nodes.", "plan": draft},
    ])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = auth(client)
        identifier = post_message(client, headers)
        failed = wait_for_conversation(
            client, identifier, headers, lambda item: item["status"] == "failed"
        )
        assert "轮次预算不足" in failed["error"]
        assert failed["task"] is None
        assert not calls
        response = client.post(
            f"{API}/{identifier}/messages",
            headers=headers,
            json={"message": "Propose a new plan with budget for analysis and delivery."},
        )
        assert response.status_code == 202
        record = pending(client, identifier, headers)
        assert record["task"]["baseline"]["budget"]["max_rounds"] == 3
        assert not record["task"]["baseline"]["confirmed"]
        assert not calls
        response = client.post(
            f"{API}/{identifier}/confirm", headers=headers, json={"version": 1, "trusted": True}
        )
        assert response.status_code == 202
        done = wait_for_conversation(
            client, identifier, headers, lambda item: item["status"] == "completed"
        )
        assert done["task"]["budget_state"]["rounds"] == 3
        assert done["task"]["budget_state"]["calls"] == 3
        assert any(item.get("id") == "json" for item in done["task"]["artifacts"])


def test_mixed_clarification_shows_safe_error_and_accepts_a_followup(tmp_path):
    app, _, calls, _ = make_agent_web(
        tmp_path,
        [
            {"action": "clarify", "text": "private-invalid-response", "gaps": [gap_data()]},
            {"action": "clarify", "text": "private-invalid-response", "gaps": [gap_data()]},
            {"action": "clarify", "text": "请明确需要采用的阈值和单位。"},
        ],
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = auth(client)
        identifier = post_message(client, headers, "Verify using demo criteria.")
        failed = wait_for_conversation(
            client, identifier, headers, lambda item: item["status"] == "failed"
        )
        assert "模型返回的决策格式" in failed["error"]
        assert "private-invalid-response" not in failed["error"]
        assert "input_value" not in failed["error"]
        assert failed["pending_decision"]["message"] == failed["error"]
        assert failed["task"] is None
        response = client.post(
            f"{API}/{identifier}/messages",
            headers=headers,
            json={"message": "Which thresholds are missing?"},
        )
        assert response.status_code == 202
        recovered = wait_for_conversation(
            client, identifier, headers, lambda item: item["status"] == "waiting_for_user"
        )
        assert recovered["error"] is None
        assert recovered["messages"][-1]["content"] == "请明确需要采用的阈值和单位。"
        assert recovered["task"] is None
        assert not calls


def test_output_binding_correction_still_requires_confirmation_before_execution(tmp_path):
    from copy import deepcopy

    from tests.agent_helpers import delivery_draft

    valid = delivery_draft()
    broken = deepcopy(valid)
    unresolved = {
        "id": "validity_explanation", "kind": "finding", "node_id": None, "item_id": None,
        "description": "Explain validity without inventing data.",
    }
    broken["requirements"]["requested_outputs"].append(unresolved)
    broken["nodes"][-1]["inputs"]["requirements"] = deepcopy(broken["requirements"])
    app, runtime, calls, _ = make_agent_web(tmp_path, [
        {"action": "plan", "text": "Incomplete binding.", "plan": broken},
        {"action": "plan", "text": "Corrected bindings.", "plan": valid},
    ])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = auth(client)
        identifier = post_message(
            client, headers, "Analyze arrays, explain validity, deliver JSON without acceptance."
        )
        record = pending(client, identifier, headers)
        assert record["error"] is None
        assert not record["task"]["baseline"]["confirmed"]
        assert record["task"]["calls"] == []
        assert not calls
        response = client.post(
            f"{API}/{identifier}/confirm", headers=headers, json={"version": 1, "trusted": True}
        )
        assert response.status_code == 202
        done = wait_for_conversation(
            client, identifier, headers, lambda item: item["status"] == "completed"
        )
        assert len(calls) == 1
        assert done["task"]["result"]["evaluation"] is None
        assert any(item.get("id") == "json" for item in done["task"]["artifacts"])
        assert len(runtime.store.recent()) == 1


def test_generic_api_confirmation_uses_v2_and_rejects_stale_or_untrusted_actions(tmp_path):
    app, _, calls, _ = make_agent_web(
        tmp_path,
        [
            {"action": "plan", "text": "Confirm supplied arrays.", "plan": draft_data()},
        ],
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = auth(client)
        identifier = post_message(client, headers)
        record = pending(client, identifier, headers)
        assert record["task"]["schema_version"] == "2.0"
        assert "scenario" not in record["task"]
        assert not calls
        endpoint = f"{API}/{identifier}/confirm"
        assert (
            client.post(endpoint, headers=headers, json={"version": 9, "trusted": True}).status_code
            == 409
        )
        assert (
            client.post(
                endpoint, headers=headers, json={"version": 1, "trusted": False}
            ).status_code
            == 422
        )
        assert client.post(f"{API}/{identifier}/resume", headers=headers).status_code == 409
        assert (
            client.post(endpoint, headers=headers, json={"version": 1, "trusted": True}).status_code
            == 202
        )
        done = wait_for_conversation(
            client, identifier, headers, lambda item: item["status"] == "completed"
        )
        assert len(calls) == 1
        assert done["task"]["result"]["evaluation"] is None
        assert done["task"]["result"]["observations"][0]["value"] == pytest.approx(2**0.5)


def test_catalog_routes_require_auth_and_are_not_parsed_as_conversation_ids(tmp_path):
    app, _, _, _ = make_agent_web(tmp_path, [])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for endpoint in (f"{API}/capabilities", "/api/resource-locations"):
            assert client.get(endpoint).status_code == 403
            response = client.get(endpoint, headers=auth(client))
            assert response.status_code == 200, response.text
        catalog = client.get(f"{API}/capabilities", headers=auth(client)).json()
        assert (
            next(item for item in catalog if item["name"] == "offline_rmse")["input_schema"][
                "additionalProperties"
            ]
            is False
        )


@pytest.mark.parametrize("case", ["token", "origin", "fetch-site", "host"])
def test_generic_entry_preserves_web_request_boundaries(tmp_path, case):
    app, runtime, calls, _ = make_agent_web(tmp_path, [])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = auth(client)
        if case == "token":
            headers["x-flight-token"] = "incorrect"
        elif case == "origin":
            headers["Origin"] = "https://elsewhere.example"
        elif case == "fetch-site":
            headers["sec-fetch-site"] = "cross-site"
        else:
            headers["Host"] = "elsewhere.example"
        result = client.post(API, headers=headers, json={"message": "Analyze."})
        assert result.status_code == (400 if case == "host" else 403)
        assert runtime.store.recent() == [] and not calls


def test_new_clarification_does_not_restore_old_failure_error(tmp_path):
    app, runtime, _, _ = make_agent_web(
        tmp_path,
        [
            {"action": "plan", "text": "Confirm analysis.", "plan": draft_data()},
            {"action": "clarify", "text": "请提供新的输入范围。"},
        ],
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = auth(client)
        identifier = post_message(client, headers)
        pending(client, identifier, headers)
        old = runtime.store.get(identifier)
        old.status = "failed"
        old.error = "Original known failure"
        runtime.store.save(old)
        assert (
            client.post(
                f"{API}/{identifier}/messages", headers=headers, json={"message": "Use new inputs."}
            ).status_code
            == 202
        )
        record = wait_for_conversation(
            client,
            identifier,
            headers,
            lambda item: (
                (item.get("pending_decision") or {}).get("message") == "请提供新的输入范围。"
            ),
        )
        assert record["error"] is None
        assert record["status"] == "waiting_for_user"
        assert record["task"]["id"] == old.task.id


def test_unknown_call_blocks_new_requirement_and_retry_without_extra_execution(tmp_path):
    from flight_agent.runtime.capabilities import Capability
    from flight_agent.runtime.capability_metadata import CapabilityMetadata, MetricDefinition
    from flight_agent.runtime.observations import CapabilityOutcome
    from tests.agent_helpers import RmseInput

    app, runtime, _, _ = make_agent_web(tmp_path, [])
    calls = []

    def unknown(context, inputs):
        from flight_agent.verification.agent_runtime import ExecutionUncertain

        calls.append(context.call_id)
        raise ExecutionUncertain("Original call is unknown.")

    tools = CapabilityRegistry(agent_mode=True)
    tools.register(
        Capability(
            name="offline_rmse",
            version="1",
            description="Controlled unknown computation",
            input_model=RmseInput,
            output_model=CapabilityOutcome,
            effect="read_only",
            operation=unknown,
            metadata=CapabilityMetadata(
                metrics=[MetricDefinition(id="rmse", label="RMSE", unit="m")],
            ),
        )
    )
    runtime.executor = AgentExecutor(runtime.store, tools, runtime.resources)
    data = draft_data()
    data.pop("resource_ids")
    data["resources"] = []
    conversation = runtime.executor.propose(runtime.store.create(), AgentPlan.model_validate(data))
    runtime.executor.confirm(conversation.id, 1, trusted=True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = auth(client)
        response = client.post(
            f"{API}/{conversation.id}/messages",
            headers=headers,
            json={"message": "Switch to something else."},
        )
        assert response.status_code == 202
        assert response.json()["task"]["id"] == conversation.task.id
        assert response.json()["status"] == "interrupted"
        assert (
            client.post(
                f"{API}/{conversation.id}/retry",
                headers=headers,
                json={"version": 1, "trusted": True},
            ).status_code
            == 409
        )
        assert len(calls) == 1


def test_model_planning_occupies_the_shared_job_slot(tmp_path, monkeypatch):
    app, runtime, _, _ = make_agent_web(tmp_path, [{"action": "answer", "text": "Done."}])
    entered, release = threading.Event(), threading.Event()
    original = runtime.planner.propose

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.planner, "propose", blocked)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        try:
            headers = auth(client)
            identifier = post_message(client, headers)
            assert entered.wait(5)
            response = client.post(API, headers=headers, json={"message": "Another."})
            assert response.status_code == 409
        finally:
            release.set()
        wait_for_conversation(
            client, identifier, headers, lambda item: item["status"] == "completed"
        )


def test_store_injection_does_not_interrupt_an_existing_active_record(tmp_path):
    from flight_agent.verification.runtime import VerificationRuntime

    store = ConversationStore(tmp_path / "state")
    record = store.create()
    record.status = "running"
    store.save(record)
    runtime = VerificationRuntime(store.root, store=store)
    assert runtime.store is store
    assert store.get(record.id).status == "running"


def test_ambiguous_directory_is_clarified_without_guessing_a_resource_kind(tmp_path):
    from flight_agent.verification.plugins import register_builtin_plugins
    from tests.diagnosis_helpers import AdaptivePlanner

    app, runtime, calls, _ = make_agent_web(tmp_path, [])
    register_builtin_plugins(
        CapabilityRegistry(agent_mode=True),
        runtime.resources,
        backend=None,
        planner=AdaptivePlanner(),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = auth(client)
        identifier = post_message(client, headers, project_root=str(tmp_path))
        record = wait_for_conversation(
            client, identifier, headers, lambda item: item["status"] == "waiting_for_user"
        )
        assert record["task"] is None
        assert record["pending_decision"]["kind"] == "clarify"
        assert not calls


def test_location_clarification_retains_the_directory_already_supplied(tmp_path):
    from flight_agent.verification.plugins import register_builtin_plugins
    from tests.diagnosis_helpers import AdaptivePlanner

    _, runtime, _, _ = make_agent_web(tmp_path, [])
    register_builtin_plugins(
        CapabilityRegistry(agent_mode=True),
        runtime.resources,
        backend=None,
        planner=AdaptivePlanner(),
    )
    record = runtime.queue_message(None, "Analyze.", str(tmp_path))
    assert record.status == "waiting_for_user"
    selected = runtime.queue_message(
        record.id, "Use the local diagnostic location.", location="agent"
    )
    assert selected.project_root == str(tmp_path)
    assert selected.resource_location == "diagnostic_data"


def test_explicitly_clearing_resource_directory_does_not_reinspect_a_previous_source(tmp_path):
    _, runtime, _, _ = make_agent_web(tmp_path, [])
    saved = runtime.store.create()
    saved.project_root = "/previous/unavailable/source"
    saved.resource_location = "retired-location"
    runtime.store.save(saved)
    queued = runtime.queue_message(
        saved.id, "Explain RMSE without accessing files.", project_root=""
    )
    assert queued.project_root is None


def test_known_failure_new_plan_reinspects_and_does_not_reuse_old_resource_fingerprint(tmp_path):
    import hashlib

    from flight_agent.directories import browse_directories
    from flight_agent.runtime.capabilities import Capability
    from flight_agent.runtime.capability_metadata import (
        CapabilityMetadata,
        MetricDefinition,
        ResourceRequirement,
    )
    from flight_agent.runtime.observations import CapabilityOutcome
    from flight_agent.runtime.resources import ResourceReference
    from flight_agent.verification.resources import ResourceAdapter
    from tests.agent_helpers import RmseInput

    data_root = tmp_path / "inputs"
    data_root.mkdir()
    file = data_root / "signals.csv"
    file.write_text("1,2\n", encoding="utf-8")
    draft = draft_data()
    draft["resource_ids"] = ["measurements"]
    draft["nodes"][0].update(
        tool="inspect_signals",
        resource_bindings={"data": "measurements"},
    )
    app, runtime, calls, tools = make_agent_web(
        tmp_path,
        [
            {"action": "plan", "text": "First analysis.", "plan": draft},
            {"action": "plan", "text": "Analyze revised signals.", "plan": draft},
        ],
    )

    def inspect(locator):
        return ResourceReference(
            id="measurements",
            kind="signal_data",
            location="agent",
            locator=locator,
            fingerprint=hashlib.sha256(file.read_bytes()).hexdigest(),
        )

    def verify(resource):
        if resource != inspect(resource.locator):
            raise ValueError("Signals changed")

    def fail(context, inputs):
        calls.append(context.call_id)
        raise RuntimeError("Known local analysis failure")

    runtime.resources.register(
        ResourceAdapter(
            name="signals",
            kind="signal_data",
            location="agent",
            inspect=inspect,
            verify=verify,
            browse=browse_directories,
            local=True,
        )
    )
    tools.register(
        Capability(
            name="inspect_signals",
            version="1",
            description="Controlled signal analysis failure",
            input_model=RmseInput,
            output_model=CapabilityOutcome,
            effect="read_only",
            operation=fail,
            metadata=CapabilityMetadata(
                metrics=[MetricDefinition(id="rmse", label="RMSE", unit="m")],
                resources=[ResourceRequirement(role="data", kind="signal_data", location="agent")],
            ),
        )
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = auth(client)
        identifier = post_message(
            client,
            headers,
            project_root=str(data_root),
            location="signals",
        )
        first = pending(client, identifier, headers)
        assert (
            client.post(
                f"{API}/{identifier}/confirm",
                headers=headers,
                json={"version": 1, "trusted": True},
            ).status_code
            == 202
        )
        failed = wait_for_conversation(
            client,
            identifier,
            headers,
            lambda item: item["status"] == "failed",
        )
        file.write_text("3,4\n", encoding="utf-8")
        assert (
            client.post(
                f"{API}/{identifier}/messages",
                headers=headers,
                json={"message": "Analyze the revised input."},
            ).status_code
            == 202
        )
        revised = pending(client, identifier, headers)
        assert revised["error"] is None
        assert revised["task"]["baseline"]["confirmed"] is False
        assert revised["task"]["baseline"]["version"] == 2
        assert (
            revised["task"]["baseline"]["resources"][0]["fingerprint"]
            != first["task"]["baseline"]["resources"][0]["fingerprint"]
        )
        assert revised["archived_tasks"][0]["calls"] == failed["task"]["calls"]
        assert len(calls) == 1


def test_resume_cannot_discard_a_followup_clarification_for_a_completed_task(tmp_path):
    app, runtime, calls, _ = make_agent_web(
        tmp_path,
        [
            {"action": "plan", "text": "Analyze.", "plan": draft_data()},
            {"action": "clarify", "text": "Provide an explicit acceptance threshold."},
        ],
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = auth(client)
        identifier = post_message(client, headers)
        pending(client, identifier, headers)
        assert (
            client.post(
                f"{API}/{identifier}/confirm",
                headers=headers,
                json={"version": 1, "trusted": True},
            ).status_code
            == 202
        )
        wait_for_conversation(
            client,
            identifier,
            headers,
            lambda item: item["status"] == "completed",
        )
        assert (
            client.post(
                f"{API}/{identifier}/messages",
                headers=headers,
                json={"message": "Now verify compliance."},
            ).status_code
            == 202
        )
        clarification = wait_for_conversation(
            client,
            identifier,
            headers,
            lambda item: (item.get("pending_decision") or {}).get("kind") == "clarify",
        )
        assert client.post(f"{API}/{identifier}/resume", headers=headers).status_code == 409
        assert runtime.store.get(identifier).model_dump(mode="json") == clarification
        assert len(calls) == 1


def test_repeat_retry_after_success_is_rejected_without_changing_the_record(tmp_path):
    from flight_agent.runtime.observations import CapabilityOutcome, Observation
    from tests.agent_helpers import register_rmse

    app, runtime, _, _ = make_agent_web(
        tmp_path,
        [
            {"action": "plan", "text": "Analyze.", "plan": draft_data()},
        ],
    )
    calls = []

    def fail_once(context, inputs):
        calls.append(context.call_id)
        if len(calls) == 1:
            raise RuntimeError("Known local failure")
        return CapabilityOutcome(
            observations=[
                Observation(
                    node_id=context.node_id, metric_id="rmse", label="RMSE", value=1, unit="m"
                ),
            ]
        )

    tools = CapabilityRegistry(agent_mode=True)
    register_rmse(tools, calls, operation=fail_once)
    runtime.executor = AgentExecutor(runtime.store, tools, runtime.resources)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = auth(client)
        identifier = post_message(client, headers)
        pending(client, identifier, headers)
        client.post(
            f"{API}/{identifier}/confirm",
            headers=headers,
            json={"version": 1, "trusted": True},
        )
        wait_for_conversation(
            client,
            identifier,
            headers,
            lambda item: item["status"] == "failed",
        )
        assert (
            client.post(
                f"{API}/{identifier}/retry",
                headers=headers,
                json={"version": 1, "trusted": True},
            ).status_code
            == 202
        )
        done = wait_for_conversation(
            client,
            identifier,
            headers,
            lambda item: item["status"] == "completed",
        )
        assert (
            client.post(
                f"{API}/{identifier}/retry",
                headers=headers,
                json={"version": 1, "trusted": True},
            ).status_code
            == 409
        )
        assert runtime.store.get(identifier).model_dump(mode="json") == done
        assert len(calls) == 2


def test_capability_gap_is_structured_and_persists_in_message_history(tmp_path):
    gap = {
        "requirement_id": "frequency-response",
        "missing_capability": "frequency-response analysis",
        "missing_inputs": ["frequency grid"],
        "limitation": "No registered tool supports this response.",
        "next_step": "Install and register an authorized adapter.",
    }
    app, runtime, calls, _ = make_agent_web(
        tmp_path,
        [
            {"action": "capability_gap", "text": "Cannot compute this response.", "gaps": [gap]},
            {"action": "answer", "text": "A frequency grid specifies evaluation frequencies."},
        ],
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = auth(client)
        identifier = post_message(client, headers, "Compute frequency response.")
        record = wait_for_conversation(
            client,
            identifier,
            headers,
            lambda item: item["status"] == "waiting_for_user",
        )
        assert record["pending_decision"]["kind"] == "capability_gap"
        assert record["messages"][-1]["gaps"] == [gap]
        assert record["messages"][-1]["response_kind"] == "capability_gap"
        assert (
            client.post(
                f"{API}/{identifier}/messages",
                headers=headers,
                json={"message": "Explain a frequency grid."},
            ).status_code
            == 202
        )
        wait_for_conversation(
            client,
            identifier,
            headers,
            lambda item: item["status"] == "completed",
        )
        restored = ConversationStore(runtime.store.root).get(identifier)
        assert restored.model_dump(mode="json")["messages"][1]["gaps"] == [gap]
        assert not calls
