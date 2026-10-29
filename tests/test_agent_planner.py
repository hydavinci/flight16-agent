import importlib
import json

import pytest
from langchain_core.messages import AIMessage
from pydantic import PrivateAttr

from flight_agent.runtime.agent_contracts import AgentPlanDraft
from flight_agent.runtime.capabilities import CapabilityRegistry
from flight_agent.runtime.requirements import CriterionSource, TaskRequirements
from flight_agent.runtime.resources import ResourceReference
from flight_agent.verification.adapters.evaluation import scalar_evaluation_capability
from flight_agent.verification.contracts import ConversationMessage
from flight_agent.verification.store import ConversationStore, now
from tests.agent_helpers import analysis_plan_data, gap_data, register_rmse, resource_data
from tests.helpers import ScriptedModel, tool_message
from tests.test_agent_evaluation import evaluation_plan


class CapturingModel(ScriptedModel):
    _seen: list = PrivateAttr(default_factory=list)

    def _generate(self, messages, **kwargs):
        self._seen.append(messages)
        return super()._generate(messages, **kwargs)


def module():
    return importlib.import_module("flight_agent.verification.agent_planner")


def setup(tmp_path, response, message="Report RMSE."):
    tools = CapabilityRegistry(agent_mode=True)
    calls = []
    register_rmse(tools, calls)
    tools.register(scalar_evaluation_capability())
    store = ConversationStore(tmp_path / "state")
    conversation = store.create()
    conversation.messages.append(
        ConversationMessage(role="user", content=message, created_at=now())
    )
    store.save(conversation)
    model = CapturingModel(responses=[tool_message("propose_agent", response)])
    return module().AgentPlanner(model=model), store, conversation, tools, calls, model


def draft(data=None):
    data = data or analysis_plan_data()
    return {**{key: value for key, value in data.items() if key != "resources"}, "resource_ids": []}


def source(conversation, quote):
    return CriterionSource(
        kind="user_message",
        reference=f"message:{conversation.id}:0",
        quote=quote,
    )


@pytest.mark.parametrize("action", ["answer", "clarify", "capability_gap"])
def test_non_plan_decisions_never_compute_or_persist_a_task(tmp_path, action):
    response = {"action": action, "text": "Need an explicit RMSE threshold."}
    if action == "capability_gap":
        response["gaps"] = [gap_data()]
    planner, store, conversation, tools, calls, _ = setup(tmp_path, response)
    decision = planner.propose(
        "Verify RMSE.",
        conversation,
        catalog=tools.catalog(),
        resources={},
        sources={},
    )
    assert decision.action == action
    assert decision.plan is None
    assert store.get(conversation.id).task is None
    assert calls == []


def test_analysis_plan_has_no_forced_criteria_judge_or_design(tmp_path):
    planner, store, conversation, tools, calls, _ = setup(
        tmp_path, {"action": "plan", "text": "Proposed analysis.", "plan": draft()}
    )
    result = planner.propose(
        "Report RMSE.",
        conversation,
        catalog=tools.catalog(),
        resources={},
        sources={},
    )
    plan = module().bind_plan(result.plan, {}, {}, tools.catalog())
    assert plan.requirements.mode == "analysis"
    assert plan.requirements.criteria == []
    assert plan.evaluation_node is None
    assert calls == []
    assert store.get(conversation.id).task is None


@pytest.mark.parametrize("external_output", [False, True])
def test_output_location_is_visible_to_planning_without_disclosing_the_path(
    tmp_path, external_output
):
    proposed = draft()
    if external_output:
        proposed["allowed_effects"].append("write_delivery")
    planner, store, conversation, tools, calls, model = setup(
        tmp_path, {"action": "plan", "text": "Proposed analysis.", "plan": proposed}
    )
    directory = tmp_path / "private-results"
    conversation.output_root = str(directory) if external_output else None
    result = planner.propose(
        "Report RMSE.", conversation, catalog=tools.catalog(), resources={}, sources={}
    )
    context = json.loads(model._seen[0][-1].content)
    assert context["has_output_directory"] is external_output
    assert str(directory) not in str(model._seen)
    assert result.plan.allowed_effects == (
        ("read_only", "write_delivery") if external_output else ("read_only",)
    )
    assert not directory.exists()
    assert store.get(conversation.id).task is None
    assert not calls


def test_missing_external_output_permission_gets_one_bounded_plan_correction(tmp_path):
    broken = draft()
    corrected = draft()
    corrected["allowed_effects"].append("write_delivery")
    planner, store, conversation, tools, calls, model = setup(tmp_path, {})
    directory = tmp_path / "private-results"
    conversation.output_root = str(directory)
    model.responses = [
        tool_message("propose_agent", {"action": "plan", "text": "Analysis.", "plan": broken}),
        tool_message("propose_agent", {"action": "plan", "text": "Confirm.", "plan": corrected}),
    ]
    result = planner.propose(
        "Report RMSE.", conversation, catalog=tools.catalog(), resources={}, sources={}
    )
    assert result.action == "plan"
    assert result.plan.allowed_effects == ("read_only", "write_delivery")
    assert result.plan.requirements.model_dump(mode="json") == broken["requirements"]
    assert [node.tool for node in result.plan.nodes] == ["offline_rmse"]
    assert len(model._seen) == 2
    original = json.loads(model._seen[0][-1].content)
    repaired = json.loads(model._seen[1][-1].content)
    feedback = repaired.pop("validation_feedback")
    assert feedback["errors"][0]["code"] == "external_output_permission_required"
    assert feedback["errors"][0]["location"] == ["allowed_effects"]
    assert repaired == original
    assert not directory.exists()
    assert store.get(conversation.id).task is None
    assert not calls


def test_repeated_missing_output_permission_clarifies_without_granting_or_writing(tmp_path):
    planner, store, conversation, tools, calls, model = setup(
        tmp_path, {"action": "plan", "text": "Analysis.", "plan": draft()}
    )
    directory = tmp_path / "private-results"
    conversation.output_root = str(directory)
    result = planner.propose(
        "Report RMSE.", conversation, catalog=tools.catalog(), resources={}, sources={}
    )
    assert result.action == "clarify"
    assert result.plan is None
    assert "write_delivery" in result.text
    assert len(model._seen) == 2
    assert not directory.exists()
    assert store.get(conversation.id).task is None
    assert not calls


def test_explicit_single_metric_threshold_is_not_replaced_by_demo_defaults(tmp_path):
    text = "Require RMSE <= 7.25 m."
    data = evaluation_plan()
    planner, _, conversation, tools, calls, model = setup(tmp_path, {}, text)
    supplied = source(conversation, text)
    data["requirements"]["criteria"][0].update(
        threshold=7.25, source=supplied.model_dump(mode="json")
    )
    data["nodes"][1]["inputs"]["criteria"] = data["requirements"]["criteria"]
    model.responses = [
        tool_message(
            "propose_agent",
            {
                "action": "plan",
                "text": "Only the requested criterion.",
                "plan": draft(data),
            },
        )
    ]
    result = planner.propose(
        text,
        conversation,
        catalog=tools.catalog(),
        resources={},
        sources={supplied.reference: supplied},
    )
    plan = module().bind_plan(result.plan, {}, {supplied.reference: supplied}, tools.catalog())
    assert len(plan.requirements.criteria) == 1
    assert plan.requirements.criteria[0].threshold == 7.25
    assert plan.requirements.criteria[0].source == supplied
    assert calls == []


@pytest.mark.parametrize(
    "mutation", ["empty-criteria", "unknown-tool", "unknown-version", "bad-source"]
)
def test_invalid_model_plan_is_not_repaired_or_executed(tmp_path, mutation):
    text = "Require RMSE <= 2 m."
    planner, store, conversation, tools, calls, model = setup(tmp_path, {}, text)
    supplied = source(conversation, text)
    data = evaluation_plan()
    data["requirements"]["criteria"][0]["source"] = supplied.model_dump(mode="json")
    data["nodes"][1]["inputs"]["criteria"] = data["requirements"]["criteria"]
    if mutation == "empty-criteria":
        data["requirements"]["criteria"] = []
    elif mutation == "unknown-tool":
        data["nodes"][0]["tool"] = "made_up_compute"
    elif mutation == "unknown-version":
        data["nodes"][0]["tool_version"] = "999"
    else:
        data["requirements"]["criteria"][0]["source"]["reference"] = "message:other:0"
    model.responses = [
        tool_message(
            "propose_agent",
            {
                "action": "plan",
                "text": "Invalid plan.",
                "plan": draft(data),
            },
        )
    ]
    with pytest.raises(ValueError):
        planner.propose(
            text,
            conversation,
            catalog=tools.catalog(),
            resources={},
            sources={supplied.reference: supplied},
        )
    assert calls == []
    assert store.get(conversation.id).task is None


@pytest.mark.parametrize("mutation", ["kind", "quote", "digest", "version", "missing"])
def test_criterion_sources_cannot_be_forged(mutation):
    data = evaluation_plan()["requirements"]
    supplied = CriterionSource(
        kind="standard_file",
        reference="standard#limit",
        quote="RMSE must be <= 2 m.",
        sha256="a" * 64,
        version="edition-1",
    )
    candidate = supplied.model_dump(mode="json")
    if mutation == "kind":
        candidate["kind"] = "accepted_example"
    elif mutation == "quote":
        candidate["quote"] = "RMSE must be <= 200 m."
    elif mutation == "digest":
        candidate["sha256"] = "b" * 64
    elif mutation == "version":
        candidate["version"] = "edition-2"
    data["criteria"][0]["source"] = candidate
    with pytest.raises(ValueError, match="source|Source"):
        module().validate_sources(
            TaskRequirements.model_validate(data),
            {} if mutation == "missing" else {supplied.reference: supplied},
        )


def test_source_clause_may_be_an_exact_substring_but_not_a_rewritten_sentence():
    data = evaluation_plan()["requirements"]
    supplied = CriterionSource(
        kind="user_message",
        reference="message:example:0",
        quote="Analyze both cases. Require RMSE <= 2 m. Deliver JSON.",
    )
    data["criteria"][0]["source"] = {
        **supplied.model_dump(mode="json"),
        "quote": "Require RMSE <= 2 m.",
    }
    module().validate_sources(TaskRequirements.model_validate(data), {supplied.reference: supplied})


def test_two_cases_preserve_distinct_thresholds_and_sources(tmp_path):
    text = "Case A RMSE <= 2.4 m. Case B RMSE <= 7.25 m."
    planner, _, conversation, tools, calls, model = setup(tmp_path, {}, text)
    supplied = source(conversation, text)
    data = evaluation_plan()
    data["nodes"].insert(1, {**data["nodes"][0], "id": "case-b"})
    first = data["requirements"]["criteria"][0]
    first.update(threshold=2.4, source={**supplied.model_dump(), "quote": "Case A RMSE <= 2.4 m."})
    data["requirements"]["criteria"].append(
        {
            **first,
            "id": "case-b-limit",
            "metric": {"node_id": "case-b", "metric_id": "rmse"},
            "threshold": 7.25,
            "source": {**supplied.model_dump(), "quote": "Case B RMSE <= 7.25 m."},
        }
    )
    data["nodes"][2].update(
        depends_on=["compute", "case-b"],
        bindings={},
        inputs={"criteria": data["requirements"]["criteria"], "overall_requested": True},
    )
    model.responses = [
        tool_message(
            "propose_agent",
            {
                "action": "plan",
                "text": "Both cases.",
                "plan": draft(data),
            },
        )
    ]
    result = planner.propose(
        text,
        conversation,
        catalog=tools.catalog(),
        resources={},
        sources={supplied.reference: supplied},
    )
    assert [item.threshold for item in result.plan.requirements.criteria] == [2.4, 7.25]
    assert calls == []


def test_resource_locator_is_bound_locally_not_accepted_from_the_model(tmp_path):
    planner, _, conversation, tools, calls, model = setup(tmp_path, {})
    resource = ResourceReference.model_validate(resource_data())
    data = draft()
    data["resource_ids"] = [resource.id]
    model.responses = [
        tool_message(
            "propose_agent",
            {
                "action": "plan",
                "text": "Use the registered input.",
                "plan": data,
            },
        )
    ]
    result = planner.propose(
        "Report RMSE.",
        conversation,
        catalog=tools.catalog(),
        resources={resource.id: resource},
        sources={},
    )
    assert resource.locator not in str(model._seen)
    bound = module().bind_plan(result.plan, {resource.id: resource}, {}, tools.catalog())
    assert bound.resources == [resource]
    data["resources"] = [resource.model_dump(mode="json")]
    with pytest.raises(ValueError):
        AgentPlanDraft.model_validate(data)
    assert calls == []


def test_long_request_retains_last_threshold_and_redacts_paths_and_credentials(tmp_path):
    message = "Analyze " + "x" * 2500 + " RMSE <= 2.4 m. /private/project secret=never-forward"
    planner, _, conversation, tools, _, model = setup(
        tmp_path, {"action": "clarify", "text": "Specify the input arrays."}, message
    )
    planner.propose(message, conversation, catalog=tools.catalog(), resources={}, sources={})
    context = json.loads(model._seen[0][-1].content)
    assert "RMSE <= 2.4 m." in context["request"]
    assert "/private/project" not in str(model._seen)
    assert "never-forward" not in str(model._seen)


def test_oversized_context_clarifies_without_silently_truncating_or_calling_model(tmp_path):
    message = "x" * 6001 + " RMSE <= 2.4 m."
    planner, _, conversation, tools, calls, model = setup(
        tmp_path, {"action": "answer", "text": "Should not be called."}
    )
    decision = planner.propose(
        message, conversation, catalog=tools.catalog(), resources={}, sources={}
    )
    assert decision.action == "clarify"
    assert "limit" in decision.text.lower() or "长" in decision.text
    assert model._seen == []
    assert calls == []


def test_unregistered_standard_file_source_is_not_sent_to_model(tmp_path):
    planner, _, conversation, tools, _, model = setup(
        tmp_path, {"action": "answer", "text": "Unused"}
    )
    supplied = CriterionSource(
        kind="standard_file",
        reference="unregistered#limit",
        quote="Private standard.",
        sha256="a" * 64,
    )
    with pytest.raises(ValueError, match="source|resource|registered"):
        planner.propose(
            "Verify.",
            conversation,
            catalog=tools.catalog(),
            resources={},
            sources={supplied.reference: supplied},
        )
    assert model._seen == []


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "answer", "text": "Answer.", "plan": draft()},
        {"action": "clarify", "text": "Question.", "gaps": [gap_data()]},
        {"action": "capability_gap", "text": "Unsupported."},
        {"action": "plan", "text": "No actual plan."},
    ],
)
def test_decision_branches_have_no_ambiguous_executable_payload(payload):
    with pytest.raises(ValueError):
        module().AgentDecision.model_validate(payload)


def test_model_visible_decision_schema_includes_branch_constraints():
    schema = module().AgentDecision.model_json_schema()
    branches = schema["oneOf"]
    assert len(branches) == 3
    non_plan, gap, plan = [branch["properties"] for branch in branches]
    assert non_plan["action"]["enum"] == ["answer", "clarify"]
    assert non_plan["plan"] == {"type": "null"}
    assert non_plan["gaps"]["maxItems"] == 0
    assert gap["action"]["const"] == "capability_gap"
    assert gap["plan"] == {"type": "null"}
    assert gap["gaps"]["minItems"] == 1
    assert branches[1]["required"] == ["gaps"]
    assert plan["action"]["const"] == "plan"
    assert plan["plan"] == {"not": {"type": "null"}}
    assert plan["gaps"]["maxItems"] == 0
    assert branches[2]["required"] == ["plan"]


@pytest.mark.parametrize(
    "response",
    [
        {"action": "clarify", "text": "Need thresholds.", "plan": None, "gaps": [gap_data()]},
        {"action": "answer", "text": "private-invalid-response", "plan": draft()},
        {"action": "plan", "text": "private-invalid-response"},
    ],
)
def test_invalid_model_decision_stops_after_one_correction_without_execution(tmp_path, response):
    planner, store, conversation, tools, calls, model = setup(tmp_path, response)
    with pytest.raises(ValueError, match="模型返回.*格式") as error:
        planner.propose(
            "Report RMSE.", conversation, catalog=tools.catalog(), resources={}, sources={}
        )
    assert "未生成新方案或触发执行" in str(error.value)
    assert "private-invalid-response" not in str(error.value)
    assert "input_value" not in str(error.value)
    assert len(model._seen) == 2
    assert calls == []
    assert store.get(conversation.id).task is None


def test_missing_output_producer_is_corrected_with_feedback_and_original_request(tmp_path):
    request = "Report RMSE and its validity without inventing missing values."
    corrected = draft()
    corrected["requirements"]["requested_outputs"][0]["description"] = request
    broken = draft()
    broken["requirements"]["requested_outputs"].append({
        "id": "validity_explanation", "kind": "finding",
        "node_id": None, "item_id": None, "description": "private-invalid-response",
    })
    planner, store, conversation, tools, calls, model = setup(tmp_path, {}, request)
    model.responses = [
        tool_message("propose_agent", {"action": "plan", "text": "Invalid.", "plan": broken}),
        tool_message("propose_agent", {"action": "plan", "text": "Corrected.", "plan": corrected}),
    ]
    decision = planner.propose(
        request, conversation, catalog=tools.catalog(), resources={}, sources={}
    )
    assert decision.action == "plan"
    assert decision.plan.requirements.requested_outputs[0].description == request
    assert len(model._seen) == 2
    original = json.loads(model._seen[0][-1].content)
    repair = json.loads(model._seen[1][-1].content)
    assert repair.pop("validation_feedback")["errors"][0]["message"].find(
        "validity_explanation"
    ) >= 0
    assert repair == original
    assert "private-invalid-response" not in str(model._seen)
    assert not calls
    assert store.get(conversation.id).task is None


@pytest.mark.parametrize("elapsed,expected_calls", [(4, 2), (11, 1)])
@pytest.mark.parametrize("failure", ["invalid-format", "external-output-permission"])
def test_correction_shares_the_original_timeout(
    tmp_path, monkeypatch, elapsed, expected_calls, failure
):
    clock = [100.0]
    monkeypatch.setattr(module(), "monotonic", lambda: clock[0])
    planner, _, conversation, tools, calls, _ = setup(tmp_path, {})
    broken = {"action": "plan", "text": "Missing draft."}
    if failure == "external-output-permission":
        conversation.output_root = str(tmp_path / "external")
        broken["plan"] = draft()

    class TimedModel(CapturingModel):
        _timeouts: list = PrivateAttr(default_factory=list)

        def _generate(self, messages, **kwargs):
            self._timeouts.append(kwargs.get("timeout"))
            clock[0] += elapsed
            return super()._generate(messages, **kwargs)

    model = TimedModel(responses=[
        tool_message("propose_agent", broken),
        tool_message("propose_agent", {"action": "clarify", "text": "Provide input."}),
    ])
    planner.model = model
    bounded = planner.with_timeout(10)
    if expected_calls == 1:
        with pytest.raises(ValueError, match="总超时"):
            bounded.propose(
                "Report RMSE.", conversation, catalog=tools.catalog(), resources={}, sources={}
            )
        assert model._timeouts == [10]
    else:
        decision = bounded.propose(
            "Report RMSE.", conversation, catalog=tools.catalog(), resources={}, sources={}
        )
        assert decision.action == "clarify"
        assert model._timeouts == [10, 6]
    assert len(model._seen) == expected_calls
    assert planner._timeout_seconds is None
    assert calls == []


@pytest.mark.parametrize(
    "failure", [TimeoutError("Model timed out"), RuntimeError("Transport error")]
)
def test_model_transport_failure_is_not_retried(tmp_path, failure):
    planner, _, conversation, tools, calls, _ = setup(tmp_path, {})

    class FailingModel(CapturingModel):
        def _generate(self, messages, **kwargs):
            self._seen.append(messages)
            raise failure

    model = FailingModel(responses=[])
    planner.model = model
    with pytest.raises(type(failure), match=str(failure)):
        planner.propose(
            "Report RMSE.", conversation, catalog=tools.catalog(), resources={}, sources={}
        )
    assert len(model._seen) == 1
    assert calls == []


def test_correction_feedback_excludes_payload_values_and_redacts_local_paths(tmp_path):
    planner, _, conversation, tools, calls, model = setup(tmp_path, {})
    conversation.project_root = "/private/project"
    model.responses = [
        tool_message("propose_agent", {
            "action": "clarify", "text": "Provide input.",
            "/private/project": {"api_key": "private-invalid-secret"},
        }),
        tool_message("propose_agent", {"action": "clarify", "text": "Provide input."}),
    ]
    decision = planner.propose(
        "Report RMSE.", conversation, catalog=tools.catalog(), resources={}, sources={}
    )
    assert decision.action == "clarify"
    feedback = json.loads(model._seen[1][-1].content)["validation_feedback"]
    assert feedback["errors"][0]["code"] == "extra_forbidden"
    assert "/private/project" not in str(model._seen)
    assert "private-invalid-secret" not in str(model._seen)
    assert calls == []


def test_correction_does_not_truncate_request_to_fit_feedback(tmp_path, monkeypatch):
    planner, _, conversation, tools, calls, _ = setup(tmp_path, {})

    class SizeLimitedModel(CapturingModel):
        def _generate(self, messages, **kwargs):
            monkeypatch.setattr(
                module(), "MAX_CONTEXT_BYTES", len(messages[-1].content.encode("utf-8")) + 1
            )
            return super()._generate(messages, **kwargs)

    model = SizeLimitedModel(responses=[
        tool_message("propose_agent", {"action": "plan", "text": "Missing draft."}),
        tool_message("propose_agent", {"action": "clarify", "text": "Unused."}),
    ])
    planner.model = model
    with pytest.raises(ValueError, match="大小限制"):
        planner.propose(
            "Report RMSE.", conversation, catalog=tools.catalog(), resources={}, sources={}
        )
    assert len(model._seen) == 1
    assert calls == []


def test_corrected_plan_still_cannot_use_an_unregistered_capability(tmp_path):
    planner, _, conversation, tools, calls, model = setup(tmp_path, {})
    forged = draft()
    forged["nodes"][0]["tool"] = "unregistered"
    model.responses = [
        tool_message("propose_agent", {"action": "plan", "text": "Missing draft."}),
        tool_message("propose_agent", {"action": "plan", "text": "Forged.", "plan": forged}),
    ]
    with pytest.raises(ValueError, match="not registered"):
        planner.propose(
            "Report RMSE.", conversation, catalog=tools.catalog(), resources={}, sources={}
        )
    assert len(model._seen) == 2
    assert calls == []


def test_shared_structured_boundary_rejects_plain_text_instead_of_guessing_a_plan(tmp_path):
    planner, _, conversation, tools, _, model = setup(tmp_path, {})
    model.responses = [AIMessage(content="I computed a passing result.")]
    with pytest.raises(ValueError, match="structured"):
        planner.propose(
            "Report RMSE.", conversation, catalog=tools.catalog(), resources={}, sources={}
        )


def test_registered_schema_references_are_not_redacted_as_local_paths(tmp_path):
    planner, _, conversation, tools, _, model = setup(
        tmp_path,
        {"action": "clarify", "text": "Provide arrays."},
    )
    catalog = tools.catalog()
    planner.propose("Report RMSE.", conversation, catalog=catalog, resources={}, sources={})
    payload = json.loads(model._seen[0][-1].content)
    assert payload["catalog"][1]["input_schema"] == catalog[1]["input_schema"]
    assert payload["catalog"][0]["output_schema"] == catalog[0]["output_schema"]


def test_forged_user_source_version_is_refused_before_model_call(tmp_path):
    planner, _, conversation, tools, _, model = setup(
        tmp_path,
        {"action": "answer", "text": "Unused."},
    )
    supplied = source(conversation, "Report RMSE.")
    supplied = CriterionSource.model_validate({**supplied.model_dump(), "version": "invented"})
    with pytest.raises(ValueError, match="source"):
        planner.propose(
            "Report RMSE.",
            conversation,
            catalog=tools.catalog(),
            resources={},
            sources={supplied.reference: supplied},
        )
    assert model._seen == []


def test_standard_clauses_require_matching_registered_resource_digest_and_version(tmp_path):
    data = evaluation_plan()
    resource = ResourceReference.model_validate(
        resource_data(
            id="standard",
            kind="standard",
            version="edition-1",
        )
    )
    supplied = CriterionSource(
        kind="standard_file",
        reference="standard#limit",
        quote="Require RMSE <= 2 m.",
        sha256=resource.fingerprint,
        version=resource.version,
    )
    data["requirements"]["criteria"][0]["source"] = supplied.model_dump()
    data["nodes"][1]["inputs"]["criteria"] = data["requirements"]["criteria"]
    data = draft(data)
    data["resource_ids"] = ["standard"]
    planner, _, conversation, tools, _, _ = setup(
        tmp_path,
        {
            "action": "plan",
            "text": "Use the explicitly supplied clause.",
            "plan": data,
        },
    )
    decision = planner.propose(
        "Verify against the registered clause.",
        conversation,
        catalog=tools.catalog(),
        resources={resource.id: resource},
        sources={supplied.reference: supplied},
    )
    assert decision.plan.requirements.criteria[0].source == supplied


def test_unavailable_capability_is_not_accepted_as_an_executable_plan(tmp_path):
    planner, _, conversation, tools, _, _ = setup(
        tmp_path,
        {
            "action": "plan",
            "text": "Unavailable plan.",
            "plan": draft(),
        },
    )
    catalog = tools.catalog()
    catalog[0]["availability"] = {"state": "unavailable", "reason": "Backend not installed"}
    with pytest.raises(ValueError, match="Backend not installed"):
        planner.propose("Compute.", conversation, catalog=catalog, resources={}, sources={})


def test_file_criterion_source_identity_is_part_of_the_bound_baseline(tmp_path):
    supplied = CriterionSource(
        kind="standard_file",
        reference="standard#limit",
        quote="Require RMSE <= 2 m.",
        sha256="a" * 64,
        version="edition-1",
    )
    tools = CapabilityRegistry(agent_mode=True)
    register_rmse(tools, [])
    tools.register(scalar_evaluation_capability())
    data = evaluation_plan()
    data["requirements"]["criteria"][0]["source"] = supplied.model_dump()
    resource = ResourceReference.model_validate(
        resource_data(id="standard", kind="standard", version="edition-1")
    )
    with pytest.raises(ValueError, match="source|resource"):
        module().bind_plan(
            AgentPlanDraft.model_validate(draft(data)),
            {resource.id: resource},
            {supplied.reference: supplied},
            tools.catalog(),
        )


def test_nested_metadata_credentials_are_not_forwarded_to_the_model(tmp_path):
    planner, _, conversation, tools, _, model = setup(
        tmp_path,
        {"action": "clarify", "text": "Need arrays."},
    )
    resource = ResourceReference.model_validate(
        resource_data(
            metadata={
                "api_key": "synthetic-private-key",
                "headers": {"Authorization": "Bearer synthetic-auth"},
                "nested": [{"password": "synthetic-password"}],
                "unit": "m",
            }
        )
    )
    planner.propose(
        "Compute.",
        conversation,
        catalog=tools.catalog(),
        resources={resource.id: resource},
        sources={},
    )
    text = str(model._seen)
    for secret in ["synthetic-private-key", "synthetic-password"]:
        assert secret not in text
    payload = json.loads(model._seen[0][-1].content)
    assert payload["resources"][0]["metadata"]["unit"] == "m"
    assert payload["catalog"][1]["input_schema"] == tools.catalog()[1]["input_schema"]


def test_redacted_source_roundtrip_restores_authenticated_quote_and_evaluation_inputs(tmp_path):
    text = "At /private/case, require RMSE <= 2 m."
    planner, _, conversation, tools, _, model = setup(tmp_path, {}, text)
    supplied = source(conversation, text)
    data = evaluation_plan()
    data["requirements"]["criteria"][0]["source"] = {
        **supplied.model_dump(mode="json"),
        "quote": "At [local path], require RMSE <= 2 m.",
    }
    data["nodes"][1]["inputs"]["criteria"] = data["requirements"]["criteria"]
    model.responses = [
        tool_message(
            "propose_agent",
            {
                "action": "plan",
                "text": "Confirm the sourced criterion.",
                "plan": draft(data),
            },
        )
    ]
    decision = planner.propose(
        text,
        conversation,
        catalog=tools.catalog(),
        resources={},
        sources={supplied.reference: supplied},
    )
    assert "/private/case" not in str(model._seen)
    assert decision.plan.requirements.criteria[0].source == supplied
    assert decision.plan.nodes[1].inputs["criteria"][0]["source"]["quote"] == text


def test_redacted_quote_does_not_authorize_a_changed_digest(tmp_path):
    text = "At /private/case, require RMSE <= 2 m."
    planner, _, conversation, tools, _, model = setup(tmp_path, {}, text)
    supplied = source(conversation, text)
    data = evaluation_plan()
    data["requirements"]["criteria"][0]["source"] = {
        **supplied.model_dump(mode="json"),
        "quote": "At [local path], require RMSE <= 2 m.",
        "sha256": "a" * 64,
    }
    model.responses = [
        tool_message(
            "propose_agent",
            {
                "action": "plan",
                "text": "Invalid source.",
                "plan": draft(data),
            },
        )
    ]
    with pytest.raises(ValueError, match="source"):
        planner.propose(
            text,
            conversation,
            catalog=tools.catalog(),
            resources={},
            sources={supplied.reference: supplied},
        )
