import json
from copy import deepcopy
from dataclasses import replace
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from flight_agent.runtime.agent_contracts import AgentPlanDraft
from flight_agent.runtime.capability_metadata import Availability
from flight_agent.runtime.contracts import fingerprint
from flight_agent.verification.agent_planner import AgentDecision, AgentPlanner, bind_plan
from flight_agent.verification.local_matlab import LocalMatlabBackend
from flight_agent.verification.matlab_contracts import LocalMatlabResult
from flight_agent.web import create_web_app
from tests.agent_helpers import wait_for_conversation
from tests.matlab_project_helpers import prepared_project

API = "/api/conversations"
REQUEST = (
    "Execute the existing model with workspace input signal: command (deg), "
    "workspace response signal: response (deg/s), condition: cruise. "
    "Use stepinfo settling band 5%; overshoot <=10%; settling_time <=3 s. Deliver JSON and Word."
)


class ControlledBackend(LocalMatlabBackend):
    """No Engine or subprocess: dispatch has deliberately unverifiable status."""

    def __init__(self):
        super().__init__("/test/selected-python")
        self.requests = []
        self.ready = True

    def availability(self):
        import os

        if os.environ.get("MATLAB_ALLOW_TRUSTED_PROJECTS") != "1":
            return Availability(state="unavailable", reason="Set MATLAB_ALLOW_TRUSTED_PROJECTS=1")
        return Availability(
            state="available" if self.ready else "unavailable",
            reason="" if self.ready else "Selected interpreter has no MATLAB Engine",
        )

    def execute(self, request, directory, *, resume=False):
        self.requests.append((request, resume))
        return LocalMatlabResult(
            id=request.id,
            request_sha256=fingerprint(request.model_dump(mode="json")),
            execution_status="unknown",
            valid=False,
            diagnostics=["Controlled unknown execution; reconcile original ID only."],
        )


def local_draft(context):
    resource = context["resources"][0]
    project = resource["metadata"]["project"]
    source = context["sources"][0]
    criteria = [
        {
            "id": metric,
            "metric": {"node_id": "measure", "metric_id": metric},
            "operator": "<=",
            "threshold": threshold,
            "unit": unit,
            "conditions": {"condition": "cruise"},
            "source": source,
        }
        for metric, threshold, unit in [("overshoot", 10.0, "%"), ("settling_time", 3.0, "s")]
    ]
    requirements = {
        "objective": "Verify existing project, retaining original source and actual evidence.",
        "mode": "verification",
        "criteria": criteria,
        "overall_requested": True,
        "delivery_formats": ["json", "docx"],
        "requested_outputs": [
            {
                "id": name,
                "kind": "metric",
                "node_id": "measure",
                "item_id": name,
                "description": "Measured " + name,
            }
            for name in ("overshoot", "settling_time")
        ]
        + [
            {
                "id": name,
                "kind": "artifact",
                "node_id": "deliver",
                "item_id": name,
                "description": "Requested delivery " + name,
            }
            for name in ("json", "docx")
        ],
    }
    return {
        "title": "Local existing-entry verification",
        "rationale": "Existing configuration, explicit calculation band, four dispatches.",
        "requirements": requirements,
        "resource_ids": [resource["id"]],
        "allowed_effects": ["read_only", "execute_project", "write_delivery"],
        "budget": {"max_rounds": 4, "max_calls": 4, "max_seconds": 60, "max_no_progress": 2},
        "evaluation_node": "evaluate",
        "nodes": [
            {
                "id": "execute",
                "tool": "matlab_execute_project",
                "tool_version": "1",
                "resource_bindings": {"project": resource["id"]},
                "inputs": {
                    "mode": "simulink",
                    "model": project["models"][0],
                    "initializer": project["entries"][0],
                    "input_signal": {"source": "workspace", "path": ["command"], "unit": "deg"},
                    "response_signal": {
                        "source": "workspace",
                        "path": ["response"],
                        "unit": "deg/s",
                    },
                    "conditions": {"condition": "cruise"},
                },
            },
            {
                "id": "measure",
                "tool": "matlab_stepinfo",
                "tool_version": "1",
                "depends_on": ["execute"],
                "bindings": {"series": {"node_id": "execute", "path": ["details", "series"]}},
                "inputs": {"settling_band": 0.05, "conditions": {"condition": "cruise"}},
            },
            {
                "id": "evaluate",
                "tool": "evaluate_criteria",
                "tool_version": "1",
                "depends_on": ["measure"],
                "bindings": {"observations": {"node_id": "measure", "path": ["observations"]}},
                "inputs": {"criteria": criteria, "overall_requested": True},
            },
            {
                "id": "deliver",
                "tool": "deliver_results",
                "tool_version": "1",
                "depends_on": ["execute", "measure", "evaluate"],
                "inputs": {
                    "title": "Local evidence",
                    "formats": ["json", "docx"],
                    "requirements": requirements,
                },
            },
        ],
    }


@pytest.fixture
def local_planning_case(tmp_path, monkeypatch):
    import sys

    import flight_agent.web as web

    def forbidden(*args, **kwargs):
        raise AssertionError("Planning must start zero Engines")

    engine = ModuleType("matlab.engine")
    engine.start_matlab = forbidden
    monkeypatch.setitem(sys.modules, "matlab.engine", engine)
    monkeypatch.setattr(LocalMatlabBackend, "_spawn", forbidden)
    monkeypatch.delenv("MATLAB_WORKER_URL", raising=False)
    monkeypatch.delenv("MATLAB_WORKER_TOKEN", raising=False)
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    project = prepared_project(tmp_path / "source")
    backend = ControlledBackend()
    monkeypatch.setattr(LocalMatlabBackend, "from_environment", classmethod(lambda cls: backend))
    planner = AgentPlanner()
    contexts = []
    proposal = {"build": local_draft}

    def decision(context, paths):
        contexts.append(deepcopy(context))
        return AgentDecision(
            action="plan",
            text="Please confirm exact entries and sources.",
            plan=AgentPlanDraft.model_validate(proposal["build"](context)),
        )

    monkeypatch.setattr(planner, "_decision", decision)
    monkeypatch.setattr(web, "AgentPlanner", lambda: planner)
    assembled = []
    original = web.EngineeringRuntime

    def capture(*args, **kwargs):
        runtime = original(*args, **kwargs)
        assembled.append(runtime)
        return runtime

    monkeypatch.setattr(web, "EngineeringRuntime", capture)
    app = create_web_app(tmp_path)
    runtime = assembled[0]
    # Keep registration failures as behavior failures rather than fixture errors.
    resource = None
    if "matlab_project" in {item.id for item in runtime.resources.locations()}:
        resource = runtime.resources.inspect("matlab_project", str(project))
    return app, runtime, resource, backend, contexts, proposal, project


def submit(case, message=REQUEST):
    _, runtime, resource, *_ = case
    assert resource is not None, "Default Web assembly must register local matlab_project"
    record = runtime.queue_message(
        None,
        message,
        resource.locator,
        location="matlab_project",
    )
    return runtime.continue_submission(record.id)


def test_local_resource_does_not_require_worker(local_planning_case):
    _, runtime, resource, backend, *_ = local_planning_case
    assert resource is not None, "Local resource missing from default Web assembly"
    catalog = runtime.executor.tools.catalog((resource,))
    assert {item["name"] for item in catalog} >= {
        "matlab_execute_project",
        "matlab_stepinfo",
        "evaluate_criteria",
        "deliver_results",
    }
    assert (resource.kind, resource.location, resource.version) == ("matlab_project", "agent", "1")
    assert resource.metadata["project"]["root"] == resource.locator
    assert resource.metadata["project"]["fingerprint"] == resource.fingerprint
    assert backend.requests == []
    locations = {item.id: item for item in runtime.resources.locations()}
    assert locations["simulink_project"].location == "worker"
    assert locations["diagnostic_data"].location == "agent"


def test_provenance_private_locator_and_four_node_bindings(local_planning_case):
    record = submit(local_planning_case, REQUEST + " Project " + str(local_planning_case[6]))
    _, _, resource, backend, contexts, *_ = local_planning_case
    baseline = record.task.baseline
    assert not baseline.confirmed and record.task.calls == []
    assert [node.tool for node in baseline.nodes] == [
        "matlab_execute_project",
        "matlab_stepinfo",
        "evaluate_criteria",
        "deliver_results",
    ]
    assert baseline.nodes[1].bindings["series"].path == ["details", "series"]
    assert baseline.nodes[1].inputs["settling_band"] == 0.05
    assert [item.threshold for item in baseline.requirements.criteria] == [10.0, 3.0]
    assert baseline.requirements.criteria[0].source.quote == record.messages[0].content
    assert baseline.budget.max_calls == baseline.budget.max_rounds == 4
    public = contexts[-1]["resources"][0]
    assert resource.locator not in json.dumps(contexts[-1])
    assert public["metadata"]["project"]["entries"][0]["evidence"] == ["initialize.m:2"]
    assert public["metadata"]["project"]["settings"]["model_parameters"]
    assert public["fingerprint"] == resource.fingerprint
    assert "flight-agent.project.json" not in {
        item["path"] for item in public["metadata"]["project"]["files"]
    }
    assert not backend.requests


@pytest.mark.parametrize(
    "damage",
    [
        "band",
        "band-conflict",
        "missing-band-source",
        "entry",
        "signature",
        "arity",
        "outputs",
        "return-without-output",
        "series-array",
        "series-path",
        "series-producer",
        "conditions",
        "signal",
        "signal-unit",
        "signal-source",
        "omitted-initializer",
        "model-ambiguity",
        "entry-ambiguity",
        "steady-state",
    ],
)
def test_invalid_actual_proposals_stop_before_confirmation(local_planning_case, damage):
    _, _, _, backend, _, proposal, project = local_planning_case
    message = REQUEST
    if damage == "band-conflict":
        (project / "initialize.m").write_text("settling_band = 0.02;\n")
    elif damage == "model-ambiguity":
        (project / "another.slx").write_bytes((project / "plant.slx").read_bytes())
        (project / "README.md").write_text("Two equally supported models; ask the user.")
    elif damage == "entry-ambiguity":
        (project / "another.m").write_text("x = 1;")
        (project / "README.md").write_text("Two equally supported initialization entries.")
    elif damage in {"arity", "outputs", "return-without-output"}:
        (project / "initialize.m").write_text(
            {
                "arity": "function initialize(required)\nend\n",
                "outputs": "function [a,b] = initialize()\nend\n",
                "return-without-output": "function initialize()\nend\n",
            }[damage]
        )
    elif damage == "missing-band-source":
        message = REQUEST.replace("stepinfo settling band 5%;", "stepinfo;")

    def broken(context):
        data = local_draft(context)
        execute, metric = data["nodes"][:2]
        if damage == "band":
            metric["inputs"]["settling_band"] = 0.10
        elif damage == "entry":
            execute["inputs"]["initializer"]["path"] = "absent.m"
        elif damage == "signature":
            execute["inputs"]["initializer"]["signature"] = "function invented()"
        elif damage == "return-without-output":
            execute["inputs"]["mode"] = "entry"
            execute["inputs"]["entry"] = execute["inputs"].pop("initializer")
            execute["inputs"].pop("model")
            execute["inputs"]["response_signal"]["source"] = "return"
        elif damage == "series-array":
            metric["bindings"] = {}
            metric["inputs"]["series"] = {"input": [0, 1], "response": [0, 1]}
        elif damage == "series-path":
            metric["bindings"]["series"]["path"] = ["details", "raw"]
        elif damage == "series-producer":
            metric["bindings"]["series"]["node_id"] = "unrelated"
            metric["depends_on"] = ["unrelated"]
            data["nodes"].insert(
                0,
                {
                    "id": "unrelated",
                    "tool": "evaluate_criteria",
                    "tool_version": "1",
                    "inputs": {"criteria": [], "observations": [], "overall_requested": False},
                },
            )
            data["budget"].update(max_calls=5, max_rounds=5)
        elif damage == "conditions":
            metric["inputs"]["conditions"] = {"condition": "invented"}
        elif damage == "signal":
            execute["inputs"]["response_signal"]["path"] = ["imaginary"]
        elif damage == "signal-unit":
            execute["inputs"]["response_signal"]["unit"] = "deg"
        elif damage == "signal-source":
            execute["inputs"]["response_signal"]["source"] = "logsout"
        elif damage == "omitted-initializer":
            execute["inputs"].pop("initializer")
        elif damage == "steady-state":
            metric["inputs"]["steady_state"] = {"tail_fraction": 0.3}
        return data

    proposal["build"] = broken
    record = submit(local_planning_case, message)
    assert record.task is None
    assert record.pending_decision.kind in {"clarify", "capability_gap"}
    assert not backend.requests


def test_followup_selection_and_original_quotes_survive_recent_history(local_planning_case):
    _, runtime, _, backend, contexts, proposal, project = local_planning_case
    (project / "other.slx").write_bytes((project / "plant.slx").read_bytes())
    (project / "README.md").write_text("Candidates plant.slx and other.slx; no chosen model.")
    first = submit(local_planning_case)
    assert first.task is None and first.pending_decision.kind == "clarify"
    proposal["build"] = _configured_draft
    selection = "Select model plant.slx and initializer initialize.m."
    for text in [selection, *["Retain my previous selection."] * 4]:
        queued = runtime.queue_message(first.id, text)
        first = runtime.continue_submission(queued.id)
    assert first.task is not None and not first.task.baseline.confirmed
    assert first.task.baseline.requirements.criteria[0].source.quote == REQUEST
    assert first.task.baseline.nodes[0].inputs["model"] == "plant.slx"
    assert any(selection == item["content"] for item in contexts[-1]["clarification_context"])
    assert all(not task.baseline.confirmed for task in first.archived_tasks)
    assert not backend.requests


@pytest.mark.parametrize("unavailable", ["engine", "consent"])
def test_discovery_available_but_compute_gap(local_planning_case, monkeypatch, unavailable):
    _, runtime, resource, backend, *_ = local_planning_case
    if unavailable == "engine":
        backend.ready = False
    else:
        monkeypatch.delenv("MATLAB_ALLOW_TRUSTED_PROJECTS")
    assert resource is not None
    assert runtime.resources.inspect("matlab_project", resource.locator) == resource
    record = submit(local_planning_case)
    assert record.task is None and record.pending_decision.kind == "capability_gap"
    assert record.messages[-1].gaps
    assert not backend.requests


def test_http_exact_consent_unknown_resume_never_redispatches(local_planning_case):
    app, runtime, resource, backend, *_ = local_planning_case
    assert resource is not None
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        response = client.post(
            API,
            headers=headers,
            json={
                "message": REQUEST,
                "project_root": resource.locator,
                "location": "matlab_project",
            },
        )
        assert response.status_code == 202
        identifier = response.json()["id"]
        record = wait_for_conversation(
            client, identifier, headers, lambda item: item["status"] == "waiting_for_user"
        )
        assert record["task"]["calls"] == [] and not backend.requests
        for consent in [{"version": 2, "trusted": True}, {"version": 1, "trusted": False}]:
            response = client.post(f"{API}/{identifier}/confirm", headers=headers, json=consent)
            assert response.status_code in {409, 422}
            assert not backend.requests
        response = client.post(
            f"{API}/{identifier}/confirm", headers=headers, json={"version": 1, "trusted": True}
        )
        assert response.status_code == 202
        record = wait_for_conversation(
            client, identifier, headers, lambda item: item["status"] == "interrupted"
        )
        assert record["task"]["baseline"]["confirmed"]
        original = backend.requests[0][0]
        runtime.decide(identifier, "resume")
        assert backend.requests == [(original, False), (original, True)]
        assert len(runtime.store.get(identifier).task.calls) == 1


@pytest.mark.parametrize("change", ["source", "selection", "capability"])
def test_changed_confirmation_scope_executes_nothing(local_planning_case, change):
    record = submit(local_planning_case)
    _, runtime, _, backend, _, _, project = local_planning_case
    if change == "source":
        (project / "initialize.m").write_text("StopTime = 20;\n")
    elif change == "selection":
        record.task.nodes[0].inputs["model"] = "other.slx"
        runtime.store.save(record)
    else:
        tools = runtime.executor.tools
        entry = tools._entries["matlab_execute_project"]
        tools._entries["matlab_execute_project"] = replace(
            entry,
            definition=entry.definition.model_copy(update={"version": "2"}),
        )
    with pytest.raises(ValueError):
        runtime.decide(record.id, "confirm", version=1, trusted=True)
    assert not backend.requests


def test_optional_existing_manifest_is_explicit_local_configuration(local_planning_case):
    _, runtime, _, backend, contexts, _, project = local_planning_case
    (project / "other.slx").write_bytes((project / "plant.slx").read_bytes())
    (project / "README.md").unlink()
    configuration = {
        "name": "Existing explicit configuration",
        "model": "plant.slx",
        "initializer": "initialize.m",
        "initializer_kind": "script",
        "input_signal": {"name": "command", "unit": "deg"},
        "response_signal": {"name": "response", "unit": "deg/s"},
    }
    (project / "flight-agent.project.json").write_text(json.dumps(configuration))
    # The controlled model must choose the configured, not lexically first, candidate.
    local_planning_case[5]["build"] = lambda context: _configured_draft(context)
    record = submit(local_planning_case, REQUEST.replace("workspace", "logsout"))
    assert record.task is not None
    facts = contexts[-1]["resources"][0]["metadata"]["project"]["settings"]
    assert facts["explicit_configuration"]["value"] == configuration
    assert facts["explicit_configuration"]["path"] == "flight-agent.project.json"
    assert record.task.baseline.nodes[0].inputs["model"] == "plant.slx"
    assert not backend.requests and runtime.legacy.backend is None


def _configured_draft(context):
    data = local_draft(context)
    data["nodes"][0]["inputs"]["model"] = "plant.slx"
    if "explicit_configuration" in context["resources"][0]["metadata"]["project"]["settings"]:
        for label in ("input_signal", "response_signal"):
            data["nodes"][0]["inputs"][label]["source"] = "logsout"
    return data


def test_pure_existing_entry_analysis_is_not_forced_to_stepinfo(local_planning_case):
    _, _, _, backend, _, proposal, project = local_planning_case
    for name in ("plant.slx", "README.md"):
        (project / name).unlink()

    def analysis(context):
        resource = context["resources"][0]
        return {
            "title": "Existing entry analysis",
            "rationale": "No acceptance requested.",
            "resource_ids": [resource["id"]],
            "allowed_effects": ["execute_project"],
            "requirements": {
                "objective": "Retain existing raw output",
                "mode": "analysis",
                "requested_outputs": [
                    {
                        "id": "raw",
                        "kind": "artifact",
                        "node_id": "execute",
                        "item_id": "raw",
                        "description": "Existing analysis output",
                    },
                ],
            },
            "nodes": [
                {
                    "id": "execute",
                    "tool": "matlab_execute_project",
                    "tool_version": "1",
                    "resource_bindings": {"project": resource["id"]},
                    "inputs": {
                        "mode": "entry",
                        "entry": resource["metadata"]["project"]["entries"][0],
                    },
                }
            ],
        }

    proposal["build"] = analysis
    record = submit(local_planning_case, "Run existing initialize.m; retain its raw result.")
    assert record.task.baseline.requirements.mode == "analysis"
    assert len(record.task.nodes) == 1 and record.task.baseline.evaluation_node is None
    assert not backend.requests


def test_bind_plan_rejects_source_mismatched_band_even_without_model(local_planning_case):
    record = submit(local_planning_case)
    _, runtime, resource, _, contexts, *_ = local_planning_case
    from flight_agent.verification.agent_planner import message_sources

    data = local_draft(contexts[-1])
    data["nodes"][1]["inputs"]["settling_band"] = 0.10
    with pytest.raises(ValueError, match="band|调节带"):
        bind_plan(
            AgentPlanDraft.model_validate(data),
            {resource.id: resource},
            message_sources(record),
            runtime.executor.tools.catalog((resource,)),
        )


@pytest.mark.parametrize("declared", [False, True])
def test_condition_requires_named_source_not_unrelated_threshold(local_planning_case, declared):
    _, _, _, backend, _, proposal, _ = local_planning_case

    def with_mach(context):
        data = local_draft(context)
        for node in data["nodes"][:2]:
            node["inputs"]["conditions"]["Mach"] = 3
        for criterion in data["requirements"]["criteria"]:
            criterion["conditions"]["Mach"] = 3
        return data

    proposal["build"] = with_mach
    record = submit(local_planning_case, REQUEST + (" Mach: 3." if declared else ""))
    if declared:
        assert record.task is not None
    else:
        assert record.task is None and record.pending_decision.kind == "clarify"
    assert not backend.requests


def test_project_settling_time_threshold_conflict_stops_confirmation(local_planning_case):
    _, _, _, backend, _, _, project = local_planning_case
    with (project / "initialize.m").open("a") as stream:
        stream.write("SettlingTimeThreshold = 0.02;\n")
    record = submit(local_planning_case)
    assert record.task is None and record.pending_decision.kind == "clarify"
    assert not backend.requests


def test_labeled_entry_and_initializer_are_selected_independently(local_planning_case):
    _, _, _, backend, _, proposal, project = local_planning_case
    (project / "analyze.m").write_text("result = 1;\n")

    def entry_analysis(context):
        data = local_draft(context)
        execute = data["nodes"][0]
        entries = context["resources"][0]["metadata"]["project"]["entries"]
        execute["inputs"].update(
            mode="entry",
            entry=next(entry for entry in entries if entry["path"] == "analyze.m"),
            initializer=next(entry for entry in entries if entry["path"] == "initialize.m"),
        )
        execute["inputs"].pop("model")
        return data

    proposal["build"] = entry_analysis
    record = submit(
        local_planning_case, REQUEST + " analysis entry: analyze.m; initializer: initialize.m"
    )
    assert record.task is not None
    assert record.task.nodes[0].inputs["entry"]["path"] == "analyze.m"
    assert record.task.nodes[0].inputs["initializer"]["path"] == "initialize.m"
    assert not backend.requests


@pytest.mark.parametrize("signal_source", ["logsout", "workspace"])
def test_manifest_signal_namespace_is_reused_without_repeated_input(
    local_planning_case,
    signal_source,
):
    _, _, _, backend, _, proposal, project = local_planning_case
    (project / "flight-agent.project.json").write_text(
        json.dumps(
            {
                "name": "Existing logged signals",
                "model": "plant.slx",
                "initializer": "initialize.m",
                "initializer_kind": "script",
                "input_signal": {"name": "command", "unit": "deg"},
                "response_signal": {"name": "response", "unit": "deg/s"},
            }
        )
    )

    def logged(context):
        data = local_draft(context)
        for label in ("input_signal", "response_signal"):
            data["nodes"][0]["inputs"][label]["source"] = signal_source
        return data

    proposal["build"] = logged
    record = submit(
        local_planning_case,
        "Use the existing project; condition: cruise. "
        "stepinfo settling band 5%; overshoot <=10%; settling_time <=3 s.",
    )
    if signal_source == "logsout":
        assert record.task is not None
    else:
        assert record.task is None and record.pending_decision.kind == "clarify"
    assert not backend.requests


@pytest.mark.parametrize(
    "quote,band",
    [
        ("Use stepinfo settling band 2%;", 0.02),
        ("采用 stepinfo，调节带为 2%；", 0.02),
        ("Use a 2% settling band;", 0.02),
        ("Stick 阶跃激励；非线性闭环时域仿真；stepinfo；调节时间 ≤3 s（2%误差带）；", 0.02),
        ("Use stepinfo SettlingTimeThreshold = 0.02;", 0.02),
    ],
)
def test_explicit_alternate_band_is_calculation_not_acceptance(local_planning_case, quote, band):
    _, _, _, backend, _, proposal, _ = local_planning_case

    def alternate(context):
        data = local_draft(context)
        data["nodes"][1]["inputs"]["settling_band"] = band
        return data

    proposal["build"] = alternate
    record = submit(local_planning_case, REQUEST.replace("Use stepinfo settling band 5%;", quote))
    assert record.task is not None
    assert record.task.nodes[1].inputs["settling_band"] == 0.02
    assert [item.threshold for item in record.task.baseline.requirements.criteria] == [10.0, 3.0]
    assert not backend.requests


@pytest.mark.parametrize("policy", ["band", "steady_state"])
def test_known_project_calculation_settings_are_reused(local_planning_case, policy):
    _, _, _, backend, _, proposal, project = local_planning_case
    message = REQUEST
    if policy == "band":
        message = REQUEST.replace("settling band 5%;", "")
        with (project / "initialize.m").open("a") as stream:
            stream.write("settling_band = 0.05;\n")
    else:
        with (project / "initialize.m").open("a") as stream:
            stream.write("tail_fraction = 0.3;\n")

        def explicit_policy(context):
            data = local_draft(context)
            data["nodes"][1]["inputs"]["steady_state"] = {"tail_fraction": 0.3}
            return data

        proposal["build"] = explicit_policy
    record = submit(local_planning_case, message)
    assert record.task is not None, record.pending_decision
    assert not backend.requests


def test_omitted_policy_cannot_override_known_project_setting(local_planning_case):
    _, _, _, backend, _, _, project = local_planning_case
    with (project / "initialize.m").open("a") as stream:
        stream.write("tail_fraction = 0.3;\n")
    record = submit(local_planning_case)
    assert record.task is None and record.pending_decision.kind == "clarify"
    assert not backend.requests


@pytest.mark.parametrize(
    "name,value,default",
    [
        ("tail_fraction", 0.3, 0.2),
        ("minimum_window_seconds", 2.0, 1.0),
        ("maximum_relative_range", 0.005, 0.02),
        ("maximum_relative_drift", 0.005, 0.01),
    ],
)
@pytest.mark.parametrize("selection", ["explicit", "omitted", "mismatch", "conflict"])
def test_user_policy_is_not_silently_replaced_by_defaults_or_project(
    local_planning_case, name, value, default, selection
):
    _, _, _, backend, _, proposal, project = local_planning_case
    message = REQUEST + f" steady_state {name}: {value};"
    if selection == "conflict":
        with (project / "initialize.m").open("a") as stream:
            stream.write(f"{name} = {default};\n")

    def policy(context):
        data = local_draft(context)
        if selection != "omitted":
            data["nodes"][1]["inputs"]["steady_state"] = {
                name: value if selection == "explicit" else default
            }
        return data

    proposal["build"] = policy
    record = submit(local_planning_case, message)
    if selection == "explicit":
        assert record.task is not None, record.pending_decision
        assert record.task.baseline.nodes[1].inputs["steady_state"][name] == value
        assert record.task.baseline.requirements.criteria[0].source.quote == message
        assert record.messages[0].content == message
    else:
        assert record.task is None
        assert record.pending_decision.kind == "clarify"
    assert not backend.requests


@pytest.mark.parametrize("authority", ["documentation", "configuration"])
@pytest.mark.parametrize("selection", ["explicit", "omitted", "user-conflict"])
def test_attributed_project_policy_cannot_be_ignored(
    local_planning_case, authority, selection
):
    _, _, _, backend, contexts, proposal, project = local_planning_case
    quote = "steady_state maximum_relative_range: 0.005;"
    if authority == "documentation":
        with (project / "README.md").open("a") as stream:
            stream.write(quote)
    else:
        (project / "flight-agent.project.json").write_text(
            json.dumps(
                {
                    "name": "Explicit validity policy",
                    "model": "plant.slx",
                    "initializer": "initialize.m",
                    "input_signal": {"name": "command", "unit": "deg"},
                    "response_signal": {"name": "response", "unit": "deg/s"},
                    "steady_state": {"maximum_relative_range": 0.005},
                }
            )
        )

    def policy(context):
        data = local_draft(context)
        if authority == "configuration":
            for label in ("input_signal", "response_signal"):
                data["nodes"][0]["inputs"][label]["source"] = "logsout"
        if selection != "omitted":
            data["nodes"][1]["inputs"]["steady_state"] = {"maximum_relative_range": 0.005}
        return data

    proposal["build"] = policy
    message = REQUEST.replace("workspace", "logsout") if authority == "configuration" else REQUEST
    if selection == "user-conflict":
        message += " maximum_relative_range: 0.01;"
    record = submit(local_planning_case, message)
    if selection == "explicit":
        assert record.task is not None, record.pending_decision
        assert record.task.baseline.nodes[1].inputs["steady_state"] == {
            "maximum_relative_range": 0.005
        }
        facts = contexts[-1]["resources"][0]["metadata"]["project"]["settings"]
        if authority == "documentation":
            assert any(quote in item["text"] for item in facts["documentation"])
        else:
            assert '"maximum_relative_range": 0.005' in facts["explicit_configuration"]["quote"]
    else:
        assert record.task is None
        assert record.pending_decision.kind == "clarify"
    assert not backend.requests


def _raw_entry_draft(context):
    data = local_draft(context)
    entries = context["resources"][0]["metadata"]["project"]["entries"]
    data["requirements"] = {
        "objective": "Existing raw analysis",
        "mode": "analysis",
        "requested_outputs": [
            {
                "id": "raw",
                "kind": "artifact",
                "node_id": "execute",
                "item_id": "raw",
                "description": "Existing analysis output",
            }
        ],
    }
    data.pop("evaluation_node")
    data["nodes"] = data["nodes"][:1]
    data["nodes"][0]["inputs"] = {
        "mode": "entry",
        "entry": next(entry for entry in entries if entry["path"] == "analyze.m"),
    }
    return data


@pytest.mark.parametrize("authority", ["user", "documentation", "configuration"])
@pytest.mark.parametrize("selection", ["explicit", "omitted", "signature"])
def test_entry_mode_requires_only_the_attributed_initializer(
    local_planning_case, authority, selection
):
    _, _, _, backend, _, proposal, project = local_planning_case
    (project / "analyze.m").write_text("result = 1;\n")
    (project / "README.md").write_text("Existing raw analysis entry: analyze.m\n")
    message = "Run analysis entry: analyze.m; retain its raw result."
    quote = "initializer: initialize.m"
    if authority == "user":
        message += " " + quote
    elif authority == "documentation":
        with (project / "README.md").open("a") as stream:
            stream.write(quote)
    else:
        (project / "flight-agent.project.json").write_text(
            json.dumps(
                {
                    "name": "Explicit initialization",
                    "model": "plant.slx",
                    "initializer": "initialize.m",
                    "input_signal": {"name": "command", "unit": "deg"},
                    "response_signal": {"name": "response", "unit": "deg/s"},
                }
            )
        )

    def analysis(context):
        data = _raw_entry_draft(context)
        if selection != "omitted":
            entries = context["resources"][0]["metadata"]["project"]["entries"]
            initializer = deepcopy(next(e for e in entries if e["path"] == "initialize.m"))
            if selection == "signature":
                initializer["signature"] = "function invented()"
            data["nodes"][0]["inputs"]["initializer"] = initializer
        return data

    proposal["build"] = analysis
    record = submit(local_planning_case, message)
    if selection == "explicit":
        assert record.task is not None, record.pending_decision
        assert record.task.baseline.nodes[0].inputs["initializer"]["path"] == "initialize.m"
    else:
        assert record.task is None
        assert record.pending_decision.kind == "clarify"
    assert not backend.requests


def test_raw_entry_with_other_candidates_needs_no_unspecified_initializer(local_planning_case):
    _, _, _, backend, _, proposal, project = local_planning_case
    (project / "analyze.m").write_text("result = 1;\n")
    (project / "README.md").unlink()
    proposal["build"] = _raw_entry_draft
    record = submit(local_planning_case, "Run analysis entry: analyze.m; retain its raw result.")
    assert record.task is not None, record.pending_decision
    assert record.task.baseline.nodes[0].inputs.get("initializer") is None
    assert len(record.task.baseline.nodes) == 1
    assert not backend.requests


@pytest.mark.parametrize(
    "initial_band,correction",
    [
        ("Use stepinfo settling band 5%;", "Correction: use stepinfo settling band 2%."),
        ("Use stepinfo settling band 5%;", "Change the settling band from 5% to 2%."),
        ("Use a 5% settling band;", "Replace the 5% settling band with 2%."),
        ("采用5%误差带；", "将5%误差带改为2%误差带。"),
        ("调节带为5%；", "调节带从5%改为2%。"),
        (
            "Use SettlingTimeThreshold = 0.05;",
            "Change SettlingTimeThreshold from 0.05 to 0.02.",
        ),
    ],
)
def test_runtime_band_correction_preserves_sources_and_baseline_history(
    local_planning_case, initial_band, correction
):
    _, runtime, _, backend, contexts, proposal, _ = local_planning_case
    message = REQUEST.replace("Use stepinfo settling band 5%;", initial_band)
    first = submit(local_planning_case, message)
    original_baseline = first.task.baseline.model_dump(mode="json")

    def corrected(context):
        data = local_draft(context)
        data["nodes"][1]["inputs"]["settling_band"] = 0.02
        return data

    proposal["build"] = corrected
    record = runtime.continue_submission(runtime.queue_message(first.id, correction).id)
    assert record.task is not None, record.pending_decision
    assert record.task.baseline.version == 2
    assert record.task.baseline.nodes[1].inputs["settling_band"] == 0.02
    assert record.archived_tasks[0].baseline.model_dump(mode="json") == original_baseline
    assert record.archived_tasks[0].baseline.nodes[1].inputs["settling_band"] == 0.05
    for text in ["Retain my previous calculation settings."] * 4:
        record = runtime.continue_submission(runtime.queue_message(record.id, text).id)
        assert record.task is not None, record.pending_decision
        assert record.task.baseline.nodes[1].inputs["settling_band"] == 0.02
    assert record.task.baseline.requirements.criteria[0].source.quote == message
    assert [item.threshold for item in record.task.baseline.requirements.criteria] == [10.0, 3.0]
    quotes = [source["quote"] for source in contexts[-1]["sources"]]
    assert message in quotes and correction in quotes
    assert any(item.content == correction for item in record.messages)
    assert all(not task.baseline.confirmed for task in [record.task, *record.archived_tasks])
    assert record.task.calls == [] and not backend.requests


@pytest.mark.parametrize(
    "followup",
    [
        "Use stepinfo settling band 2%.",
        "Maybe change the settling band to 2% or 5%?",
        "Correction: use settling band 2% and settling band 5%.",
        "Do not change the settling band to 2%.",
        "If approved, change the settling band to 2%.",
    ],
)
def test_unresolved_user_band_conflicts_still_require_clarification(
    local_planning_case, followup
):
    _, runtime, _, backend, _, proposal, _ = local_planning_case
    first = submit(local_planning_case)

    def conflicting(context):
        data = local_draft(context)
        data["nodes"][1]["inputs"]["settling_band"] = 0.02
        return data

    proposal["build"] = conflicting
    record = runtime.continue_submission(runtime.queue_message(first.id, followup).id)
    assert record.pending_decision.kind == "clarify"
    assert not backend.requests


def test_unrelated_question_does_not_invalidate_an_explicit_band_correction(local_planning_case):
    _, runtime, _, backend, _, proposal, _ = local_planning_case
    first = submit(local_planning_case)

    def corrected(context):
        data = local_draft(context)
        data["nodes"][1]["inputs"]["settling_band"] = 0.02
        return data

    proposal["build"] = corrected
    record = runtime.continue_submission(
        runtime.queue_message(
            first.id, "Change the settling band to 2%; can the report be JSON or Word?"
        ).id
    )
    assert record.pending_decision.kind == "confirm_baseline"
    assert record.task.baseline.nodes[1].inputs["settling_band"] == 0.02
    assert not backend.requests


def test_user_band_correction_does_not_override_active_project_band(local_planning_case):
    _, runtime, _, backend, _, proposal, project = local_planning_case
    with (project / "initialize.m").open("a") as stream:
        stream.write("SettlingTimeThreshold = 0.02;\n")
    def current(context):
        data = local_draft(context)
        data["nodes"][1]["inputs"]["settling_band"] = 0.02
        return data

    proposal["build"] = current
    first = submit(local_planning_case, REQUEST.replace("settling band 5%", "settling band 2%"))
    assert first.task is not None
    proposal["build"] = local_draft
    record = runtime.continue_submission(
        runtime.queue_message(first.id, "Change the settling band from 2% to 5%.").id
    )
    assert record.pending_decision.kind == "clarify"
    assert not backend.requests


def test_policy_correction_is_per_field_and_survives_unrelated_followups(local_planning_case):
    _, runtime, _, backend, contexts, proposal, _ = local_planning_case

    def policy(context):
        data = local_draft(context)
        data["nodes"][1]["inputs"]["steady_state"] = {
            "maximum_relative_range": 0.005,
            "tail_fraction": 0.3,
        }
        return data

    proposal["build"] = policy
    message = REQUEST + " maximum_relative_range: 0.005; tail_fraction: 0.3;"
    first = submit(local_planning_case, message)
    assert first.task is not None

    def corrected(context):
        data = policy(context)
        data["nodes"][1]["inputs"]["steady_state"]["maximum_relative_range"] = 0.01
        return data

    proposal["build"] = corrected
    correction = "Change maximum_relative_range from 0.005 to 0.01."
    record = runtime.continue_submission(runtime.queue_message(first.id, correction).id)
    assert record.task is not None, record.pending_decision
    for text in ["Keep the same outputs."] * 4:
        record = runtime.continue_submission(runtime.queue_message(record.id, text).id)
        assert record.task is not None, record.pending_decision
        assert record.task.baseline.nodes[1].inputs["steady_state"] == {
            "maximum_relative_range": 0.01,
            "tail_fraction": 0.3,
        }
    assert record.task.baseline.nodes[1].inputs["settling_band"] == 0.05
    assert record.task.baseline.requirements.criteria[0].source.quote == message
    assert message in [source["quote"] for source in contexts[-1]["sources"]]
    assert not backend.requests


def test_policy_resolution_does_not_admit_unknown_contract_fields(local_planning_case):
    _, _, _, backend, _, proposal, _ = local_planning_case

    def unknown_policy(context):
        data = local_draft(context)
        data["nodes"][1]["inputs"]["steady_state"] = {"invented_policy": 1}
        return data

    proposal["build"] = unknown_policy
    record = submit(local_planning_case)
    assert record.task is None
    assert record.pending_decision.kind == "clarify"
    assert not backend.requests


@pytest.mark.parametrize("value", ["-0.005", "+0.005"])
def test_signed_policy_declaration_cannot_fall_back_to_default(local_planning_case, value):
    _, _, _, backend, *_ = local_planning_case
    record = submit(local_planning_case, REQUEST + f" maximum_relative_range: {value};")
    assert record.task is None
    assert record.pending_decision.kind == "clarify"
    assert not backend.requests


def test_explicit_working_directory_cannot_be_silently_omitted(local_planning_case):
    _, _, _, backend, _, _, project = local_planning_case
    (project / "flight-agent.project.json").write_text(
        json.dumps(
            {
                "name": "Configured working directory",
                "model": "plant.slx",
                "initializer": "initialize.m",
                "initializer_kind": "script",
                "working_directory": "data",
                "input_signal": {"name": "command", "unit": "deg"},
                "response_signal": {"name": "response", "unit": "deg/s"},
            }
        )
    )
    record = submit(local_planning_case)
    assert record.task is None and record.pending_decision.kind == "clarify"
    assert not backend.requests


def test_configured_working_directory_is_reused(local_planning_case):
    _, _, _, backend, _, proposal, project = local_planning_case
    (project / "flight-agent.project.json").write_text(
        json.dumps(
            {
                "name": "Configured working directory",
                "model": "plant.slx",
                "initializer": "initialize.m",
                "initializer_kind": "script",
                "working_directory": "data",
                "input_signal": {"name": "command", "unit": "deg"},
                "response_signal": {"name": "response", "unit": "deg/s"},
            }
        )
    )

    def configured(context):
        data = local_draft(context)
        data["nodes"][0]["inputs"]["working_directory"] = "data"
        for label in ("input_signal", "response_signal"):
            data["nodes"][0]["inputs"][label]["source"] = "logsout"
        return data

    proposal["build"] = configured
    record = submit(local_planning_case, REQUEST.replace("workspace", "logsout"))
    assert record.task is not None
    assert record.task.nodes[0].inputs["working_directory"] == "data"
    assert not backend.requests


def test_optional_manifest_has_original_bytes_and_new_fingerprint(local_planning_case):
    _, runtime, resource, _, _, _, project = local_planning_case
    config = {
        "name": "Original explicit config",
        "model": "plant.slx",
        "initializer": "initialize.m",
        "input_signal": {"name": "command", "unit": "deg"},
        "response_signal": {"name": "response", "unit": "deg/s"},
    }
    original = json.dumps(config, indent=3) + "\n"
    (project / "flight-agent.project.json").write_text(original)
    actual = runtime.resources.inspect("matlab_project", str(project))
    explicit = actual.metadata["project"]["settings"]["explicit_configuration"]
    assert explicit["quote"] == original and actual.fingerprint != resource.fingerprint
    with pytest.raises(ValueError, match="changed"):
        runtime.resources.verify([resource])


def test_optional_manifest_cannot_be_silently_ignored_after_metadata_truncation(
    local_planning_case,
    monkeypatch,
):
    import flight_agent.verification.matlab_projects as discovery

    _, runtime, _, _, _, _, project = local_planning_case
    (project / "README0.md").write_text("x" * 2000)
    (project / "flight-agent.project.json").write_text(
        json.dumps(
            {
                "name": "Existing configuration",
                "model": "plant.slx",
                "initializer": "initialize.m",
                "input_signal": {"name": "command", "unit": "deg"},
                "response_signal": {"name": "response", "unit": "deg/s"},
            }
        )
    )
    monkeypatch.setattr(discovery, "MAX_METADATA_BYTES", 100)
    with pytest.raises(ValueError, match="configuration|config"):
        runtime.resources.inspect("matlab_project", str(project))


def test_optional_manifest_cannot_be_ignored_after_serialized_metadata_truncation(
    local_planning_case,
    monkeypatch,
):
    import flight_agent.verification.matlab_projects as discovery

    _, runtime, _, _, _, _, project = local_planning_case
    (project / "README0.md").write_text("x" * 4000)
    (project / "flight-agent.project.json").write_text(
        json.dumps(
            {
                "name": "Existing configuration",
                "model": "plant.slx",
                "initializer": "initialize.m",
                "input_signal": {"name": "command", "unit": "deg"},
                "response_signal": {"name": "response", "unit": "deg/s"},
            }
        )
    )
    monkeypatch.setattr(discovery, "MAX_RESULT", 2000)
    with pytest.raises(ValueError, match="configuration|config"):
        runtime.resources.inspect("matlab_project", str(project))


@pytest.mark.parametrize("interpreter_ready", [True, False])
def test_default_web_probes_selected_interpreter_without_engine(
    tmp_path,
    monkeypatch,
    interpreter_ready,
):
    import os
    import sys
    from pathlib import Path

    monkeypatch.delenv("MATLAB_WORKER_URL", raising=False)
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    interpreter = sys.executable if interpreter_ready else str(tmp_path / "missing-python")
    monkeypatch.setenv("MATLAB_PYTHON_EXECUTABLE", interpreter)
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            [
                str(Path(__file__).parent / "fixtures" / "fake_local_matlab"),
                str(Path(__file__).resolve().parents[1] / "src"),
            ]
        ),
    )
    marker = tmp_path / "engine-starts"
    monkeypatch.setenv("TEST_LOCAL_STARTS", str(marker))
    spawn = LocalMatlabBackend._spawn
    probes = []

    def probe_only(self, argument, **kwargs):
        assert argument == "--probe", "Planning must not launch an execution child"
        probes.append(self.interpreter)
        return spawn(self, argument, **kwargs)

    monkeypatch.setattr(LocalMatlabBackend, "_spawn", probe_only)
    app = create_web_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
        catalog = client.get(f"{API}/capabilities", headers=headers).json()
        metric = next(item for item in catalog if item["name"] == "matlab_stepinfo")
        assert metric["availability"]["state"] == (
            "available" if interpreter_ready else "unavailable"
        )
        locations = client.get("/api/resource-locations", headers=headers).json()
        assert (
            next(item for item in locations if item["id"] == "matlab_project")["availability"][
                "state"
            ]
            == "available"
        )
    assert probes == [interpreter] and not marker.exists()
