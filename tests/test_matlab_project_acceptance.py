"""HTTP acceptance with a controlled planner and explicitly fake offline Engine.

Only the external planner decision and MATLAB process are doubles. Discovery,
binding, confirmation, subprocess lifecycle, registered capabilities, immutable
receipts, scalar evaluation and Word/JSON downloads are production code.
"""

import hashlib
import json
import os
import shutil
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from docx import Document
from fastapi.testclient import TestClient
from PIL import Image

from flight_agent.runtime.agent_contracts import AgentPlanDraft
from flight_agent.runtime.capability_metadata import Availability
from flight_agent.runtime.contracts import fingerprint
from flight_agent.runtime.evidence import verify_evidence
from flight_agent.verification.agent_planner import AgentDecision, AgentPlanner
from flight_agent.verification.local_matlab import LocalMatlabBackend
from flight_agent.verification.matlab_projects import discover_matlab_project
from flight_agent.web import create_web_app
from tests.helpers import ScriptedModel

API = "/api/conversations"


@dataclass
class PreparedCase:
    root: Path
    mode: str
    selected: str
    initializer: str | None
    namespace: str
    input_name: str
    response_name: str
    input_unit: str
    response_unit: str
    condition: str
    band: float = 0.05

    @property
    def analysis(self):
        role = "model" if self.mode == "simulink" else "analysis entry"
        initializer = f" Existing initializer: {self.initializer}." if self.initializer else ""
        semantics = (
            "Stick step; nonlinear closed-loop time simulation (fixture provider: "
            "ydot=-y+sat(2*(r-y),-0.5,0.5))."
            if self.mode == "simulink"
            else "Thermal bench oscillatory response; execute the existing MATLAB-only function."
        )
        return (
            f"{semantics} Existing {role}: {self.selected}.{initializer} "
            f"{self.namespace} input signal: {self.input_name} ({self.input_unit}); "
            f"{self.namespace} response signal: {self.response_name} ({self.response_unit}); "
            f"condition: {self.condition}. Use stepinfo; deliver Word and JSON."
        )

    @property
    def criteria(self):
        return (
            f"Overshoot <=10%; settling_time <=3 s; "
            f"stepinfo settling band {self.band * 100:g}%."
        )

    def document(self):
        (self.root / "README.md").write_text(self.analysis + "\n" + self.criteria + "\n")


def prepare_offline_case(root, kind="simulink"):
    """Static provider fixtures, not executable MATLAB; the Engine below is fake."""
    if kind == "simulink":
        case = PreparedCase(
            root, kind, "models/limited_loop.slx", "setup/prepare.m",
            "logsout", "lever", "stroke", "1", "m", "limited",
        )
        (root / "models").mkdir(parents=True)
        (root / "setup").mkdir()
        (root / case.initializer).write_text("feedback_gain = 2;\ncondition = 'limited';\n")
        with ZipFile(root / case.selected, "w") as archive:
            archive.writestr(
                "simulink/configSet0.xml",
                '<ConfigSet><P Name="StopTime">16</P>'
                '<P Name="SignalLoggingName">logsout</P></ConfigSet>',
            )
    else:
        case = PreparedCase(
            root, "entry", "analysis/thermal_response.m", None,
            "return", "signals.excitation", "signals.temperature", "V", "K", "bench",
        )
        (root / "analysis").mkdir(parents=True)
        (root / case.selected).write_text(
            "function result = thermal_response()\nresult = struct;\nend\n"
        )
    case.document()
    return case


def acceptance_draft(context, case):
    """Test-only controlled planner: no arrays, locators or invented source facts."""
    resource = context["resources"][0]
    project = resource["metadata"]["project"]
    source = next(item for item in context["sources"] if "Overshoot" in item["quote"])
    entries = {item["path"]: item for item in project["entries"]}
    execute = {
        "mode": case.mode,
        "conditions": {"condition": case.condition},
        **{
            label: {"source": case.namespace, "path": name.split("."), "unit": unit}
            for label, name, unit in [
                ("input_signal", case.input_name, case.input_unit),
                ("response_signal", case.response_name, case.response_unit),
            ]
        },
    }
    if case.mode == "simulink":
        execute.update(model=case.selected, initializer=entries[case.initializer])
    else:
        execute["entry"] = entries[case.selected]
    criteria = [
        {
            "id": metric, "metric": {"node_id": "measure", "metric_id": metric},
            "operator": "<=", "threshold": threshold, "unit": unit,
            "conditions": execute["conditions"], "source": source,
        }
        for metric, threshold, unit in [("overshoot", 10.0, "%"), ("settling_time", 3.0, "s")]
    ]
    requirements = {
        "objective": case.analysis,
        "mode": "verification", "criteria": criteria, "overall_requested": True,
        "delivery_formats": ["json", "docx"],
        "requested_outputs": [
            {"id": metric, "kind": "metric", "node_id": "measure", "item_id": metric,
             "description": "Measured " + metric}
            for metric in ("overshoot", "settling_time")
        ] + [
            {"id": fmt, "kind": "artifact", "node_id": "deliver", "item_id": fmt,
             "description": "Download " + fmt}
            for fmt in ("json", "docx")
        ],
    }
    return {
        "title": "Prepared project evidence", "rationale": "Use provider-attributed settings.",
        "resource_ids": [resource["id"]], "requirements": requirements,
        "allowed_effects": ["read_only", "execute_project", "write_delivery"],
        "budget": {"max_rounds": 4, "max_calls": 4, "max_seconds": 240, "max_no_progress": 2},
        "evaluation_node": "evaluate",
        "nodes": [
            {"id": "execute", "tool": "matlab_execute_project", "tool_version": "1",
             "resource_bindings": {"project": resource["id"]}, "inputs": execute},
            {"id": "measure", "tool": "matlab_stepinfo", "tool_version": "1",
             "depends_on": ["execute"],
             "bindings": {"series": {"node_id": "execute", "path": ["details", "series"]}},
             "inputs": {"settling_band": case.band, "conditions": execute["conditions"]}},
            {"id": "evaluate", "tool": "evaluate_criteria", "tool_version": "1",
             "depends_on": ["measure"],
             "bindings": {"observations": {"node_id": "measure", "path": ["observations"]}},
             "inputs": {"criteria": criteria, "overall_requested": True}},
            {"id": "deliver", "tool": "deliver_results", "tool_version": "1",
             "depends_on": ["execute", "measure", "evaluate"],
             "inputs": {"title": "Prepared project evidence", "formats": ["json", "docx"],
                        "requirements": requirements}},
        ],
    }


def acceptance_app(directory, monkeypatch, case, backend=None, mutate=None):
    import flight_agent.web as web

    planner = AgentPlanner(model=ScriptedModel(responses=[]))
    contexts = []

    def decision(context, paths):
        contexts.append(deepcopy(context))
        draft = acceptance_draft(context, case)
        if mutate:
            mutate(draft)
        return AgentDecision(
            action="plan", text="Confirm existing entries and attributed settings.",
            plan=AgentPlanDraft.model_validate(draft),
        )

    monkeypatch.setattr(planner, "_decision", decision)
    monkeypatch.setattr(web, "AgentPlanner", lambda: planner)
    if backend is not None:
        monkeypatch.setattr(
            LocalMatlabBackend, "from_environment", classmethod(lambda cls: backend),
        )
    assembled = []
    runtime_class = web.EngineeringRuntime

    def capture(*args, **kwargs):
        runtime = runtime_class(*args, **kwargs)
        assembled.append(runtime)
        return runtime

    monkeypatch.setattr(web, "EngineeringRuntime", capture)
    app = create_web_app(directory)
    return app, assembled[0], contexts


def wait_http(client, identifier, headers, statuses, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"{API}/{identifier}", headers=headers)
        assert response.status_code == 200, response.text
        record = response.json()
        if record["status"] in statuses:
            return record
        assert record["status"] != "failed", record["error"]
        time.sleep(0.02)
    pytest.fail(f"Timed out awaiting {statuses}: {record}")


def propose_http(client, case):
    headers = {"X-Flight-Token": client.get("/api/bootstrap").json()["token"]}
    response = client.post(API, headers=headers, json={
        "project_root": str(case.root), "location": "matlab_project",
        "analysis_request": case.analysis, "evaluation_criteria": case.criteria,
    })
    assert response.status_code == 202, response.text
    record = wait_http(client, response.json()["id"], headers, {"waiting_for_user"})
    return headers, record


def confirm_http(client, headers, record, *, timeout=15):
    assert record["task"], record["messages"][-1]
    assert record["task"]["calls"] == []
    endpoint = f"{API}/{record['id']}/confirm"
    for consent in [{"version": 99, "trusted": True}, {"version": 1, "trusted": False}]:
        response = client.post(endpoint, headers=headers, json=consent)
        assert response.status_code in {409, 422}, response.text
    response = client.post(endpoint, headers=headers, json={"version": 1, "trusted": True})
    assert response.status_code == 202, response.text
    return wait_http(
        client, record["id"], headers, {"completed", "interrupted", "failed"}, timeout=timeout,
    )


def download_results(client, headers, record):
    downloads = {}
    for fmt in ("json", "docx"):
        artifact = next(item for item in record["task"]["artifacts"] if item["id"] == fmt)
        response = client.get(
            f"{API}/{record['id']}/tasks/{record['task']['id']}/artifacts/{artifact['path']}",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert len(response.content) == artifact["size"]
        assert hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
        downloads[fmt] = response.content
    return json.loads(downloads["json"]), Document(BytesIO(downloads["docx"]))


class ControlledEngine:
    """Fake external Engine only. Metrics are prescribed observations, not MATLAB results."""

    def cd(self, path, *, nargout):
        self.support = Path(path)

    def addpath(self, path, *, nargout):
        pass

    def which(self, name, *, nargout):
        return str(self.support / (name + ".m"))

    def feval(self, name, request_path, *, nargout):
        request = json.loads(Path(request_path).read_text())
        inputs = request["inputs"]
        mode = os.environ.get("TEST_ACCEPTANCE_MODE", "")
        if mode == "unknown" or (
            mode == "unknown_stepinfo" and name == "flight_agent_local_stepinfo"
        ):
            os._exit(0)  # Test-only lost terminal receipt; never a verified success.
        if mode == "no_logs" and name == "flight_agent_run_project":
            raise RuntimeError("Missing explicitly selected logsout signal")
        paths = ["raw.mat", "runtime-metadata.json"]
        Path("raw.mat").write_bytes(b"MATLAB 5.0 MAT-file, test-only placeholder, not real MATLAB")
        runtime = {
            key: request[key] for key in ("id", "request_sha256", "snapshot_root")
        }
        runtime.update(
            working_directory=request["snapshot_root"], matlab_version="test-only-fake-engine",
            matlab_release="test-only", products=[], settings={"StopTime": "16"},
        )
        Path("runtime-metadata.json").write_text(json.dumps(runtime))
        details = {"mode": inputs.get("mode", "stepinfo")}
        valid = mode not in {"invalid", "multistep", "nonsteady"}
        diagnostics = [] if valid else [f"Test-only invalid evidence: {mode}"]
        if name == "flight_agent_run_project":
            series = {
                role: {
                    "time": [0, 0.5, 1, 2, 3, 4, 8, 16],
                    "values": values, "time_unit": "s", "unit": inputs[label]["unit"],
                    "name": ".".join(inputs[label]["path"]),
                }
                for role, label, values in [
                    ("input", "input_signal", [0, 0, 1, 1, 1, 1, 1, 1]),
                    ("response", "response_signal", [0, 0, 0, 0.3, 0.45, 0.49, 0.5, 0.5]),
                ]
            }
            Path("series.json").write_text(json.dumps(series))
        else:
            Path("series.json").write_bytes(Path(request["series_path"]).read_bytes())
            series = json.loads(Path("series.json").read_text())
            Image.new("RGB", (8, 8), "white").save("step-response.png")
            paths.append("step-response.png")
            details.update(
                settling_band=inputs["settling_band"], steady_state_policy=inputs["steady_state"],
                step_time=1.0, initial_value=0.0, final_value=0.5,
            )
            for role in ("input", "response"):
                details[role + "_signal"] = {
                    key: series[role][key] for key in ("name", "unit")
                }
        paths.append("series.json")
        bridge = {
            key: request[key] for key in ("id", "request_sha256")
        }
        bridge.update(
            artifact_paths=paths, output_evidence=True, details=details, diagnostics=diagnostics,
        )
        if name == "flight_agent_local_stepinfo":
            values = json.loads(os.environ.get("TEST_ACCEPTANCE_METRICS", "[9.5, 2.99]"))
            bridge.update(valid=valid, metrics={
                metric: {"value": value, "unit": unit}
                for metric, value, unit in [
                    ("overshoot", values[0], "%"), ("settling_time", values[1], "s"),
                ]
            } if valid else {})
        Path("bridge.json").write_text(json.dumps(bridge))

    def quit(self):
        pass


def start_controlled_matlab():
    Path("engine-starts.txt").write_text("test-only fake Engine start")
    return ControlledEngine()


@pytest.fixture
def offline_engine(tmp_path, monkeypatch):
    package = tmp_path / "fake-engine" / "matlab"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "engine.py").write_text(
        "from tests.test_matlab_project_acceptance import "
        "start_controlled_matlab as start_matlab\n"
    )
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    monkeypatch.delenv("MATLAB_PYTHON_EXECUTABLE", raising=False)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([
        str(package.parent), str(Path(__file__).resolve().parents[1]),
        str(Path(__file__).resolve().parents[1] / "src"),
    ]))
    monkeypatch.delenv("MATLAB_WORKER_URL", raising=False)
    monkeypatch.delenv("MATLAB_WORKER_TOKEN", raising=False)
    class OfflineBackend(LocalMatlabBackend):
        def availability(self):
            return Availability(state="available", reason="Test-only fake Engine")

    backend = OfflineBackend(os.sys.executable)
    monkeypatch.setattr(LocalMatlabBackend, "from_environment", classmethod(lambda cls: backend))
    return backend


@pytest.fixture
def completed_project_case(tmp_path, monkeypatch, offline_engine):
    case = prepare_offline_case(tmp_path / "limited-project")
    app, runtime, contexts = acceptance_app(tmp_path / "web", monkeypatch, case)
    before = discover_matlab_project(str(case.root))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers, pending = propose_http(client, case)
        assert not list((tmp_path / "web").rglob("engine-starts.txt"))
        done = confirm_http(client, headers, pending)
        assert done["status"] == "completed", done["error"]
        payload, word = download_results(client, headers, done)
    assert discover_matlab_project(str(case.root)) == before
    return runtime.store.get(done["id"]), payload, word, contexts, case


def test_injected_web_reuses_registered_backend_for_ui_catalog_and_execution(
    tmp_path, monkeypatch, offline_engine,
):
    case = prepare_offline_case(tmp_path / "source")
    _, runtime, _ = acceptance_app(tmp_path / "web", monkeypatch, case)
    owner = runtime.local_matlab_backend
    assert owner is offline_engine
    assert owner.snapshots is runtime.executor.snapshots
    assert not owner.snapshots.root.exists()

    def forbidden(cls):
        raise AssertionError("Injected runtime must not create an unused local backend")

    monkeypatch.setattr(LocalMatlabBackend, "from_environment", classmethod(forbidden))
    runtime.local_matlab_backend = None
    readiness_calls = []
    original_readiness = owner.readiness

    def readiness():
        readiness_calls.append(owner)
        return original_readiness()

    executions = []
    original_execute = owner.execute

    def execute(request, directory, *, resume=False):
        executions.append((owner, request.operation, owner.snapshots))
        return original_execute(request, directory, resume=resume)

    monkeypatch.setattr(owner, "readiness", readiness)
    monkeypatch.setattr(owner, "execute", execute)
    app = create_web_app(tmp_path / "web", engineering_runtime=runtime)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        bootstrap = client.get("/api/bootstrap").json()
        headers = {"X-Flight-Token": bootstrap["token"]}
        catalog = client.get(f"{API}/capabilities", headers=headers).json()
        local = next(item for item in catalog if item["name"] == "matlab_stepinfo")
        assert bootstrap["local_matlab"]["state"] == local["availability"]["state"]
        assert len(readiness_calls) >= 2
        assert all(backend is owner for backend in readiness_calls)
        assert not owner.snapshots.root.exists()
        pending = propose_http(client, case)[1]
        done = confirm_http(client, headers, pending)
        assert done["status"] == "completed", done["error"]
    assert executions
    assert all(
        backend is owner and snapshots is runtime.executor.snapshots
        for backend, _, snapshots in executions
    )


def _injected_runtime(tmp_path, capabilities, snapshots, *, declared_backend=None):
    from flight_agent.runtime.capabilities import CapabilityRegistry
    from flight_agent.verification.agent_runtime import AgentContext, AgentExecutor
    from flight_agent.verification.coordinator import EngineeringRuntime
    from flight_agent.verification.resources import ResourceRegistry
    from flight_agent.verification.store import ConversationStore

    tools: CapabilityRegistry[AgentContext] = CapabilityRegistry(agent_mode=True)
    for capability in capabilities:
        tools.register(capability)
    store = ConversationStore(tmp_path / "state")
    resources = ResourceRegistry()
    return EngineeringRuntime(
        store,
        AgentPlanner(model=ScriptedModel(responses=[])),
        AgentExecutor(store, tools, resources, snapshots=snapshots),
        resources,
        local_matlab_backend=declared_backend,
    )


@pytest.mark.parametrize("registered_owner", [None, object()])
def test_injected_local_capability_requires_registered_backend_owner(
    tmp_path, registered_owner,
):
    from flight_agent.verification.adapters.local_matlab import local_matlab_capabilities
    from flight_agent.verification.project_snapshots import ProjectSnapshotStore

    snapshots = ProjectSnapshotStore(tmp_path / "managed" / "snapshots")
    backend = LocalMatlabBackend(os.sys.executable, snapshots=snapshots)
    capability = next(
        capability
        for capability in local_matlab_capabilities(backend)
        if capability.name == "matlab_linearize"
    )
    runtime = _injected_runtime(
        tmp_path,
        [replace(capability, runtime_owner=registered_owner)],
        snapshots,
    )
    with pytest.raises(ValueError, match="runtime owner|backend owner"):
        create_web_app(tmp_path / "web", engineering_runtime=runtime)


def test_injected_runtime_derives_backend_from_linearize_only_registry(
    tmp_path, monkeypatch,
):
    from flight_agent.verification.adapters.local_matlab import local_matlab_capabilities
    from flight_agent.verification.project_snapshots import ProjectSnapshotStore

    snapshots = ProjectSnapshotStore(tmp_path / "managed" / "snapshots")
    backend = LocalMatlabBackend(os.sys.executable)
    capability = next(
        capability
        for capability in local_matlab_capabilities(backend)
        if capability.name == "matlab_linearize"
    )
    runtime = _injected_runtime(tmp_path, [capability], snapshots)

    def forbidden(cls):
        raise AssertionError("Registered linearize owner must be used")

    monkeypatch.setattr(LocalMatlabBackend, "from_environment", classmethod(forbidden))
    app = create_web_app(tmp_path / "web", engineering_runtime=runtime)
    assert backend.snapshots is snapshots
    assert not snapshots.root.exists()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        bootstrap = client.get("/api/bootstrap").json()
        headers = {"X-Flight-Token": bootstrap["token"]}
        catalog = client.get(f"{API}/capabilities", headers=headers).json()
    assert bootstrap["local_matlab"]["state"] in {"available", "unavailable"}
    assert [item["name"] for item in catalog] == ["matlab_linearize"]
    assert not snapshots.root.exists()


def test_injected_runtime_rejects_mixed_registered_local_backend_owners(tmp_path):
    from flight_agent.verification.adapters.local_matlab import local_matlab_capabilities
    from flight_agent.verification.project_snapshots import ProjectSnapshotStore

    snapshots = ProjectSnapshotStore(tmp_path / "managed" / "snapshots")
    first = LocalMatlabBackend(os.sys.executable, snapshots=snapshots)
    second = LocalMatlabBackend(os.sys.executable, snapshots=snapshots)
    first_capabilities = {
        capability.name: capability for capability in local_matlab_capabilities(first)
    }
    second_capabilities = {
        capability.name: capability for capability in local_matlab_capabilities(second)
    }
    runtime = _injected_runtime(
        tmp_path,
        [
            first_capabilities["matlab_linearize"],
            second_capabilities["matlab_stepinfo"],
        ],
        snapshots,
    )
    with pytest.raises(ValueError, match="same backend owner"):
        create_web_app(tmp_path / "web", engineering_runtime=runtime)


def test_injected_runtime_rejects_declared_registered_backend_mismatch(tmp_path):
    from flight_agent.verification.adapters.local_matlab import local_matlab_capabilities
    from flight_agent.verification.project_snapshots import ProjectSnapshotStore

    snapshots = ProjectSnapshotStore(tmp_path / "managed" / "snapshots")
    registered = LocalMatlabBackend(os.sys.executable, snapshots=snapshots)
    declared = LocalMatlabBackend(os.sys.executable, snapshots=snapshots)
    runtime = _injected_runtime(
        tmp_path,
        local_matlab_capabilities(registered),
        snapshots,
        declared_backend=declared,
    )
    with pytest.raises(ValueError, match="(?i)declared.*registered"):
        create_web_app(tmp_path / "web", engineering_runtime=runtime)


def test_injected_runtime_rejects_backend_snapshot_store_mismatch(tmp_path):
    from flight_agent.verification.adapters.local_matlab import local_matlab_capabilities
    from flight_agent.verification.project_snapshots import ProjectSnapshotStore

    backend = LocalMatlabBackend(
        os.sys.executable,
        snapshots=ProjectSnapshotStore(tmp_path / "backend" / "snapshots"),
    )
    executor_snapshots = ProjectSnapshotStore(tmp_path / "executor" / "snapshots")
    runtime = _injected_runtime(
        tmp_path,
        local_matlab_capabilities(backend),
        executor_snapshots,
    )
    with pytest.raises(ValueError, match="snapshot store"):
        create_web_app(tmp_path / "web", engineering_runtime=runtime)


def test_requested_case_delivers_real_dependency_evidence(completed_project_case):
    record, payload, word, contexts, case = completed_project_case
    task = record.task
    assert task.baseline.confirmed
    assert task.budget_state.calls == task.budget_state.rounds == 4
    assert [node.tool for node in task.baseline.nodes] == [
        "matlab_execute_project", "matlab_stepinfo", "evaluate_criteria", "deliver_results",
    ]
    assert {check.unit for check in task.result.evaluation.checks} == {"%", "s"}
    assert task.result.evaluation.status == "pass"
    assert task.result.unmet_requirements == []
    assert payload["evaluation_requested"] is True
    assert payload["unmet_requirements"] == []
    runs = dict(zip(("execute", "measure", "evaluate", "deliver"), task.analysis_runs, strict=True))
    assert [call.node_id for call in task.calls] == list(runs)
    assert runs["measure"].spec.inputs["series"] == task.outputs["execute"].details["series"]
    for run, producers in [
        (runs["execute"], []), (runs["measure"], ["execute"]),
        (runs["evaluate"], ["measure"]), (runs["deliver"], ["execute", "measure", "evaluate"]),
    ]:
        assert run.spec.dependency_output_sha256 == {
            producer: fingerprint(task.outputs[producer].model_dump(mode="json"))
            for producer in producers
        }
    dependencies = payload["evidence"]["details"]["dependencies"]
    series = dependencies["execute"]["series"]
    assert series["node_id"] == "execute" and series["id"] == "series"
    assert dependencies["measure"]["settling_band"] == 0.05
    assert dependencies["measure"]["input_signal"] == {"name": "lever", "unit": "1"}
    assert dependencies["measure"]["response_signal"] == {"name": "stroke", "unit": "m"}
    for artifact in task.artifacts:
        verify_evidence(Path(task.result_directory), artifact)
    assert case.root.as_posix() not in json.dumps(contexts)
    assert '"values":' not in json.dumps(contexts)  # No arrays supplied by the planner.
    text = "\n".join(paragraph.text for paragraph in word.paragraphs)
    assert case.analysis in text
    assert case.criteria in text
    missing = [
        value for value in (
            "settling_band", "initial_value", "final_value",
            task.baseline.resources[0].fingerprint, "test-only-fake-engine", "StopTime",
        ) if value not in text
    ]
    assert not missing, f"Word omits verified execution/calculation provenance: {missing}"
    assert word.inline_shapes, "The Word report must include the actual response curve"
    plot = next(item for item in task.artifacts if item.path.endswith("step-response.png"))
    image = word.inline_shapes[0]._inline.graphic.graphicData.pic.blipFill.blip.embed
    assert hashlib.sha256(word.part.related_parts[image].blob).hexdigest() == plot.sha256


@pytest.mark.parametrize("kind", ["simulink", "entry"])
@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([10.0, 3.0], ["pass", "pass"]),
        ([10.0000000001, 3.0], ["fail", "pass"]),
        ([10.0, 3.0000000001], ["pass", "fail"]),
        ([9.9999999999, 2.9999999999], ["pass", "pass"]),
    ],
)
def test_unrounded_observations_control_boundary_evaluation(
    tmp_path, monkeypatch, offline_engine, kind, values, expected,
):
    monkeypatch.setenv("TEST_ACCEPTANCE_METRICS", json.dumps(values))
    case = prepare_offline_case(tmp_path / "different-layout", kind)
    app, runtime, _ = acceptance_app(tmp_path / "web", monkeypatch, case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers, pending = propose_http(client, case)
        done = confirm_http(client, headers, pending)
        assert done["status"] == "completed", done["error"]
        payload, _ = download_results(client, headers, done)
    checks = runtime.store.get(done["id"]).task.result.evaluation.checks
    assert [check.value for check in checks] == values
    assert [check.status for check in checks] == expected
    assert [item["value"] for item in payload["evidence"]["observations"]] == values


@pytest.mark.parametrize("damage", ["missing_entry", "ambiguous_model", "source_change"])
def test_missing_ambiguous_or_changed_source_never_executes(
    tmp_path, monkeypatch, offline_engine, damage,
):
    case = prepare_offline_case(tmp_path / "source")
    mutate = None
    if damage == "missing_entry":
        def mutate(draft):
            draft["nodes"][0]["inputs"]["initializer"]["path"] = "absent.m"
    elif damage == "ambiguous_model":
        (case.root / "models/other.slx").write_bytes((case.root / case.selected).read_bytes())
        (case.root / "README.md").write_text("Two models; no provider selection.\n")
        case.selected = "models/other.slx"
        # Keep signal/condition/band attribution, but omit a model selection in the request.
    app, runtime, _ = acceptance_app(tmp_path / "web", monkeypatch, case, mutate=mutate)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        if damage == "ambiguous_model":
            headers = {"X-Flight-Token": client.get("/api/bootstrap").json()["token"]}
            response = client.post(API, headers=headers, json={
                "project_root": str(case.root), "location": "matlab_project",
                "analysis_request": case.analysis.replace(
                    f"Existing model: {case.selected}.", "Use the prepared model."
                ),
                "evaluation_criteria": case.criteria,
            })
            pending = wait_http(
                client, response.json()["id"], headers, {"waiting_for_user"},
            )
        else:
            headers, pending = propose_http(client, case)
        if damage == "source_change":
            (case.root / case.initializer).write_text("feedback_gain = 3;\n")
            response = client.post(
                f"{API}/{pending['id']}/confirm", headers=headers,
                json={"version": 1, "trusted": True},
            )
            assert response.status_code == 202, response.text
            stopped = wait_http(client, pending["id"], headers, {"failed"})
            assert "changed" in stopped["error"].lower()
        else:
            assert pending["task"] is None
            assert pending["pending_decision"]["kind"] in {"clarify", "capability_gap"}
    record = runtime.store.get(pending["id"])
    assert record.task is None or record.task.calls == []
    assert not list((tmp_path / "web").rglob("engine-starts.txt"))


@pytest.mark.parametrize("mode", ["no_logs", "invalid", "multistep", "nonsteady"])
def test_missing_or_invalid_evidence_cannot_pass(
    tmp_path, monkeypatch, offline_engine, mode,
):
    monkeypatch.setenv("TEST_ACCEPTANCE_MODE", mode)
    case = prepare_offline_case(tmp_path / "source")
    app, runtime, _ = acceptance_app(tmp_path / "web", monkeypatch, case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers, pending = propose_http(client, case)
        done = confirm_http(client, headers, pending)
        result = runtime.store.get(done["id"]).task.result
        if mode == "no_logs":
            assert done["status"] == "failed"
            assert result is None or result.evaluation is None
        else:
            assert done["status"] == "completed"
            payload, _ = download_results(client, headers, done)
            assert result.evaluation.status == "undetermined"
            assert all(item.status == "undetermined" for item in result.evaluation.checks)
            assert result.observations == []
            assert payload["unmet_requirements"]
            assert payload["execution_status"] == "partial"


def test_restart_unknown_original_call_reconciles_without_a_new_dispatch(
    tmp_path, monkeypatch, offline_engine,
):
    monkeypatch.setenv("TEST_ACCEPTANCE_MODE", "unknown")
    case = prepare_offline_case(tmp_path / "source")
    directory = tmp_path / "web"
    app, runtime, _ = acceptance_app(directory, monkeypatch, case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers, pending = propose_http(client, case)
        done = confirm_http(client, headers, pending)
        assert done["status"] == "interrupted"
    task = runtime.store.get(done["id"]).task
    original = task.calls[0].id
    work = runtime.store.root / "work" / done["id"] / task.id
    requests = list(work.rglob("request.json"))
    assert len(requests) == 1
    before = requests[0].read_bytes()
    # Reconstruct the actual Web/runtime against its persisted store, then resume over HTTP.
    monkeypatch.setenv("TEST_ACCEPTANCE_MODE", "")
    offline_engine.snapshots = None
    app, restored, _ = acceptance_app(directory, monkeypatch, case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"X-Flight-Token": client.get("/api/bootstrap").json()["token"]}
        response = client.post(f"{API}/{done['id']}/resume", headers=headers)
        assert response.status_code == 202, response.text
        resumed = wait_http(client, done["id"], headers, {"interrupted"})
    task = restored.store.get(resumed["id"]).task
    assert [call.id for call in task.calls] == [original]
    assert task.budget_state.calls == 1
    assert list(work.rglob("request.json")) == requests
    assert requests[0].read_bytes() == before


def test_persisted_marker_free_metric_reconciles_original_directory_after_restart(
    tmp_path, monkeypatch, offline_engine,
):
    from flight_agent.runtime.evidence import describe_evidence, publish_evidence
    from flight_agent.verification.matlab_contracts import LocalMatlabRequest

    monkeypatch.setenv("TEST_ACCEPTANCE_MODE", "unknown_stepinfo")
    case = prepare_offline_case(tmp_path / "source")
    directory = tmp_path / "web"
    app, runtime, _ = acceptance_app(directory, monkeypatch, case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers, pending = propose_http(client, case)
        done = confirm_http(client, headers, pending)
        assert done["status"] == "interrupted"

    record = runtime.store.get(done["id"])
    task = record.task
    metric_call = task.calls[-1]
    assert metric_call.status == "interrupted"
    managed_call = (
        runtime.store.root
        / "work"
        / record.id
        / task.id
        / "calls"
        / metric_call.id
    )
    managed_backend = managed_call / "local-matlab"
    request = LocalMatlabRequest.model_validate_json(
        (managed_backend / "request.json").read_bytes()
    )
    assert request.operation == "stepinfo"
    assert request.project is None and request.project_snapshot is None

    legacy_call = Path(task.result_directory) / "calls" / metric_call.id
    shutil.copytree(managed_call / "calls", legacy_call / "calls")
    legacy_backend = legacy_call / "local-matlab"
    historical = runtime.local_matlab_backend.execute(request, legacy_backend)
    assert historical.execution_status == "unknown"
    engine_start = (legacy_backend / "engine-starts.txt").read_bytes()
    shutil.rmtree(managed_call)

    checkpoint_name = task.checkpoints[metric_call.id]
    checkpoint_path = Path(task.result_directory) / checkpoint_name
    payload = json.loads(checkpoint_path.read_text())
    assert payload["data"].pop("backend_directory").endswith("/local-matlab")
    assert payload["data"]["request"] == request.model_dump(mode="json")
    content = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode()
    digest = hashlib.sha256(content).hexdigest()
    historical_checkpoint_name = (
        Path(checkpoint_name).parent / f"{digest}.json"
    ).as_posix()
    task_root = Path(task.result_directory)
    publish_evidence(task_root, historical_checkpoint_name, content)
    descriptor = describe_evidence(task_root, historical_checkpoint_name)
    task.checkpoints[metric_call.id] = historical_checkpoint_name
    task.checkpoint_sha256[metric_call.id] = descriptor.sha256
    runtime.store.save(record)

    tracked = [
        historical_checkpoint_name,
        f"calls/{metric_call.id}/local-matlab/request.json",
        f"calls/{metric_call.id}/local-matlab/state.json",
        f"calls/{metric_call.id}/local-matlab/result.json",
    ]
    before = {
        name: (Path(task.result_directory) / name).read_bytes()
        for name in tracked
    }
    state = json.loads(before[tracked[2]])
    result = json.loads(before[tracked[3]])
    assert state["request_sha256"] == fingerprint(request.model_dump(mode="json"))
    assert result["request_sha256"] == state["request_sha256"]
    assert state["result_sha256"] == hashlib.sha256(before[tracked[3]]).hexdigest()
    snapshots = runtime.executor.snapshots
    snapshot_inventory = sorted(
        path.relative_to(snapshots.root).as_posix()
        for path in snapshots.root.rglob("*")
    )
    shutil.rmtree(case.root)

    monkeypatch.setenv("TEST_ACCEPTANCE_MODE", "")
    offline_engine.snapshots = None
    app, restored, _ = acceptance_app(directory, monkeypatch, case)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"X-Flight-Token": client.get("/api/bootstrap").json()["token"]}

        def forbidden(*args, **kwargs):
            raise AssertionError("Historical reconciliation must not redispatch")

        monkeypatch.setattr(restored.local_matlab_backend, "_spawn", forbidden)
        response = client.post(f"{API}/{done['id']}/resume", headers=headers)
        assert response.status_code == 202, response.text
        resumed = wait_http(client, done["id"], headers, {"interrupted"})

    restored_task = restored.store.get(resumed["id"]).task
    assert [call.id for call in restored_task.calls] == [
        call.id for call in task.calls
    ]
    assert restored_task.budget_state.calls == task.budget_state.calls
    assert not managed_call.exists()
    assert (legacy_backend / "engine-starts.txt").read_bytes() == engine_start
    assert {
        name: (Path(task.result_directory) / name).read_bytes()
        for name in tracked
    } == before
    assert sorted(
        path.relative_to(snapshots.root).as_posix()
        for path in snapshots.root.rglob("*")
    ) == snapshot_inventory


def test_bootstrap_and_catalog_share_import_probe_without_caching_execution_permission(
    tmp_path, monkeypatch, offline_engine,
):
    case = prepare_offline_case(tmp_path / "source")
    backend = LocalMatlabBackend(os.sys.executable)
    app, _, _ = acceptance_app(tmp_path / "web", monkeypatch, case, backend)
    spawn = backend._spawn
    probes = []

    def probe_only(argument, **kwargs):
        assert argument == "--probe", "Readiness must not execute a project"
        probes.append(argument)
        return spawn(argument, **kwargs)

    monkeypatch.setattr(backend, "_spawn", probe_only)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        bootstrap = client.get("/api/bootstrap").json()
        headers = {"X-Flight-Token": bootstrap["token"]}
        catalog = client.get(f"{API}/capabilities", headers=headers).json()
        metric = next(item for item in catalog if item["name"] == "matlab_stepinfo")
        assert bootstrap["local_matlab"]["state"] == metric["availability"]["state"] == "available"
        assert len(probes) == 1
        # Execution's availability check stays fresh rather than trusting the UI import cache.
        assert backend.availability().state == "available"
        assert len(probes) == 2
        monkeypatch.delenv("MATLAB_ALLOW_TRUSTED_PROJECTS")
        assert client.get("/api/bootstrap").json()["local_matlab"]["state"] == "unavailable"
        assert backend.availability().state == "unavailable"
        assert len(probes) == 2
        monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
        assert client.get("/api/bootstrap").json()["local_matlab"]["state"] == "available"
        assert len(probes) == 3


def test_output_bounds_handles_atomic_rename_without_accepting_symlinks(tmp_path, monkeypatch):
    import flight_agent.verification.local_matlab as module

    temporary = tmp_path / "bridge.json.tmp"
    published = tmp_path / "bridge.json"
    temporary.write_text("{}")
    walk = os.walk

    def rename_after_listing(directory, *, followlinks):
        for current, directories, files in walk(directory, followlinks=followlinks):
            if temporary.name in files:
                temporary.replace(published)
            yield current, directories, files

    monkeypatch.setattr(module.os, "walk", rename_after_listing)
    module._output_bounds(tmp_path)
    assert published.read_text() == "{}"
    # A disappearing temporary file is not a reason to ignore a live unsafe artifact.
    (tmp_path / "unsafe.json").symlink_to(published)
    with pytest.raises(ValueError, match="symlink"):
        module._output_bounds(tmp_path)
