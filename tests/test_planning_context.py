from copy import deepcopy

import pytest
from pydantic import ValidationError

from flight_agent.runtime.capabilities import CapabilityRegistry
from flight_agent.runtime.capability_metadata import CapabilityMetadata, CapabilityPlanning
from flight_agent.runtime.planning_contracts import PlanningMetrics
from flight_agent.runtime.requirements import CriterionSource
from flight_agent.runtime.resources import ResourceReference
from flight_agent.verification.contracts import Conversation, ConversationMessage
from flight_agent.verification.diagnostic_planner import DiagnosticReviewer
from flight_agent.verification.planning_context import select_context
from flight_agent.verification.plugins import register_builtin_plugins
from flight_agent.verification.resources import ResourceRegistry


def selection_catalog():
    def item(name, metric_ids, kinds, dependencies=(), dynamic=False, roles=()):
        return {
            "name": name, "version": "1", "effect": "read_only",
            "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
            "metadata": {
                "metrics": [{"id": metric, "label": metric, "unit": "m"}
                            for metric in metric_ids],
                "resources": list(roles),
                "planning": {
                    "output_kinds": list(kinds), "depends_on": list(dependencies),
                    "dynamic_dependencies": dynamic,
                },
            },
            "availability": {"state": "available", "reason": ""},
        }
    return [
        item("execute", (), ("artifact",)),
        item("rmse", ("rmse",), ("metric",), ("execute",)),
        item("margin", ("phase_margin",), ("metric",)),
        item("deliver", (), ("artifact",), dynamic=True),
    ]


def selection_conversation(message="Report rmse."):
    return Conversation(
        id="a" * 32, created_at="2026-10-31", updated_at="2026-10-31",
        messages=[ConversationMessage(role="user", content=message, created_at="2026-10-31")],
    )


def test_selection_is_order_independent_and_keeps_dependency_producers():
    catalog = selection_catalog()
    original = deepcopy(catalog)
    record = selection_conversation()
    first = select_context(
        "Report rmse.", record, catalog=catalog, resources={}, sources={},
    )
    second = select_context(
        "Report rmse.", record, catalog=list(reversed(catalog)), resources={}, sources={},
    )
    assert first == second
    assert [entry["name"] for entry in first.catalog] == ["execute", "rmse"]
    assert catalog == original


@pytest.mark.parametrize("message", [
    "Report rmse and explain every issue.", "Do the usual analysis.",
    "Report a_new_metric.", "Report rmse; deliver Word.", "不要只 Report rmse.",
])
def test_uncertain_or_unrecognized_request_keeps_every_capability(message):
    catalog = selection_catalog()
    result = select_context(
        message, selection_conversation(message), catalog=catalog, resources={}, sources={},
    )
    assert {entry["name"] for entry in result.catalog} == {entry["name"] for entry in catalog}


def test_unknown_dependency_metadata_keeps_the_entire_catalog():
    catalog = selection_catalog()
    catalog[2]["metadata"].pop("planning")
    result = select_context(
        "Report rmse.", selection_conversation(), catalog=catalog, resources={}, sources={},
    )
    assert len(result.catalog) == len(catalog)


def test_missing_and_unavailable_resources_do_not_hide_a_relevant_capability():
    catalog = selection_catalog()
    catalog[1]["metadata"]["resources"] = [{
        "role": "data", "kind": "signal_data", "location": "agent", "required": True,
    }]
    catalog[1]["availability"] = {"state": "needs_input", "reason": "Missing resource role: data"}
    for state in ("needs_input", "unavailable"):
        catalog[1]["availability"]["state"] = state
        result = select_context(
            "Report rmse.", selection_conversation(), catalog=catalog, resources={}, sources={},
        )
        selected = {entry["name"]: entry for entry in result.catalog}
        assert selected["rmse"]["availability"]["state"] == state


def test_explicit_unrelated_excerpt_is_omitted_but_originals_and_unknowns_are_retained():
    record = selection_conversation()
    reference = f"message:{record.id}:0"
    sources = {
        reference: CriterionSource(
            kind="user_message", reference=reference, quote="Report rmse.",
        ),
        "standard#rmse": CriterionSource(
            kind="standard_file", reference="standard#rmse",
            quote="  RMSE <= 2 m.\n", sha256="b" * 64, version="edition-1",
        ),
        "standard#margin": CriterionSource(
            kind="standard_file", reference="standard#margin",
            quote="Phase margin >= 45 deg.", sha256="b" * 64, version="edition-1",
        ),
        "standard#unknown": CriterionSource(
            kind="standard_file", reference="standard#unknown",
            quote="Keep this unclassified conflicting requirement.",
            sha256="b" * 64, version="edition-1",
        ),
    }
    resource = ResourceReference(
        id="standard", kind="standard", location="agent", locator="/private/standard.txt",
        fingerprint="b" * 64, version="edition-1",
        metadata={"planning_source_scopes": {
            "standard#rmse": {"metric_ids": ["rmse"], "capability_names": []},
            "standard#margin": {"metric_ids": ["phase_margin"], "capability_names": []},
        }},
    )
    original = deepcopy(sources)
    result = select_context(
        "Report rmse.", record, catalog=selection_catalog(),
        resources={"standard": resource}, sources=sources,
    )
    assert {item.reference for item in result.sources} == {
        reference, "standard#rmse", "standard#unknown",
    }
    assert next(item.quote for item in result.sources if item.reference == "standard#rmse") == (
        "  RMSE <= 2 m.\n"
    )
    assert sources == original
    assert sources["standard#margin"].quote == "Phase margin >= 45 deg."


@pytest.mark.parametrize("case", ["missing-dependency", "cycle", "dynamic", "multi-metric"])
def test_dependency_closure_never_drops_required_context(case):
    catalog = selection_catalog()
    request = "Report rmse."
    expected = {"execute", "rmse"}
    if case == "missing-dependency":
        catalog[1]["metadata"]["planning"]["depends_on"] = ["not_registered"]
        expected = {entry["name"] for entry in catalog}
    elif case == "cycle":
        catalog[0]["metadata"]["planning"]["depends_on"] = ["rmse"]
    elif case == "dynamic":
        catalog[1]["metadata"]["planning"]["dynamic_dependencies"] = True
        expected = {entry["name"] for entry in catalog}
    else:
        request = "Report rmse and phase_margin."
        expected.add("margin")
    result = select_context(
        request, selection_conversation(request), catalog=catalog, resources={}, sources={},
    )
    assert {entry["name"] for entry in result.catalog} == expected


def test_followup_scope_is_not_reduced_to_the_last_message():
    record = selection_conversation("Report phase_margin.")
    record.messages.append(ConversationMessage(
        role="user", content="Report rmse.", created_at="2026-10-31",
    ))
    result = select_context(
        "Report rmse.", record, catalog=selection_catalog(), resources={}, sources={},
    )
    assert len(result.catalog) == 4


def test_source_selection_never_rewrites_the_complete_local_document(tmp_path):
    import hashlib

    text = "  RMSE <= 2 m.\n" + ("Unclassified original text.\n" * 1000)
    text += "Phase margin >= 45 deg.\n"
    document = tmp_path / "standard.txt"
    document.write_text(text, encoding="utf-8")
    before = document.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    resource = ResourceReference(
        id="standard", kind="standard", location="agent", locator=str(document),
        fingerprint=digest, version="edition-1",
        metadata={"planning_source_scopes": {
            "standard#margin": {"metric_ids": ["phase_margin"], "capability_names": []},
        }},
    )
    excerpt = CriterionSource(
        kind="standard_file", reference="standard#margin",
        quote="Phase margin >= 45 deg.", sha256=digest, version="edition-1",
    )
    result = select_context(
        "Report rmse.", selection_conversation(), catalog=selection_catalog(),
        resources={"standard": resource}, sources={excerpt.reference: excerpt},
    )
    assert result.sources == ()
    assert document.read_bytes() == before
    assert excerpt.quote in document.read_text("utf-8")


def test_missing_planning_declaration_is_unknown_not_an_empty_capability():
    assert CapabilityMetadata().planning is None
    metadata = CapabilityMetadata(planning=CapabilityPlanning(output_kinds=["metric"]))
    assert metadata.planning.depends_on == []


def test_planning_contracts_reject_duplicate_and_unbounded_values():
    with pytest.raises(ValidationError):
        CapabilityPlanning(output_kinds=["metric", "metric"])
    with pytest.raises(ValidationError):
        CapabilityPlanning(output_kinds=["metric"], depends_on=["prepare", "prepare"])
    with pytest.raises(ValidationError):
        PlanningMetrics(planning_duration_seconds=float("nan"))
    with pytest.raises(ValidationError):
        PlanningMetrics(correction_round_count=2)


def test_catalog_only_metadata_does_not_change_execution_snapshots():
    from flight_agent.runtime.capability_metadata import CapabilitySnapshot
    from flight_agent.runtime.contracts import fingerprint

    tools = CapabilityRegistry(agent_mode=True)
    register_builtin_plugins(
        tools, ResourceRegistry(), backend=None, diagnostic_reviewer=DiagnosticReviewer(),
    )
    catalog = {entry["name"]: entry for entry in tools.catalog()}
    assert catalog["matlab_stepinfo"]["metadata"]["planning"]["depends_on"] == [
        "matlab_execute_project",
    ]
    snapshot = tools.snapshot("matlab_stepinfo")
    old_payload = snapshot.model_dump(mode="json")
    assert "planning" not in old_payload["metadata"]
    restored = CapabilitySnapshot.model_validate(old_payload)
    assert fingerprint(restored.model_dump(mode="json")) == fingerprint(old_payload)
    assert restored == snapshot


def test_every_registered_capability_remains_reachable_with_uncertain_intent():
    tools = CapabilityRegistry(agent_mode=True)
    register_builtin_plugins(
        tools,
        ResourceRegistry(),
        backend=None,
        diagnostic_reviewer=DiagnosticReviewer(),
    )
    catalog = tools.catalog()
    request = "Describe all registered engineering capabilities."
    result = select_context(
        request,
        selection_conversation(request),
        catalog=catalog,
        resources={},
        sources={},
    )
    assert {entry["name"] for entry in result.catalog} == {
        entry["name"] for entry in catalog
    }
    for entry in catalog:
        assert entry["metadata"]["planning"] is not None
        for metric in entry["metadata"]["metrics"]:
            request = f"Report {metric['id']}."
            selected = select_context(
                request,
                selection_conversation(request),
                catalog=catalog,
                resources={},
                sources={},
            )
            assert entry["name"] in {item["name"] for item in selected.catalog}
