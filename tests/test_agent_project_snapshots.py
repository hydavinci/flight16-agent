from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread

import pytest

from flight_agent.runtime.agent_contracts import AgentPlan
from flight_agent.runtime.capabilities import Capability, CapabilityRegistry
from flight_agent.runtime.capability_metadata import (
    CapabilityMetadata,
    MetricDefinition,
    ResourceRequirement,
)
from flight_agent.runtime.observations import CapabilityOutcome, Finding
from flight_agent.runtime.resources import AgentContract, ResourceReference
from flight_agent.runtime.scheduling_contracts import SchedulingMetadata
from flight_agent.verification.agent_runtime import AgentExecutor
from flight_agent.verification.contracts import ConversationState
from flight_agent.verification.coordinator import EngineeringRuntime
from flight_agent.verification.matlab_projects import discover_matlab_project
from flight_agent.verification.project_snapshots import ProjectSnapshotStore
from flight_agent.verification.resources import ResourceRegistry
from flight_agent.verification.snapshot_references import retained_snapshot_ids
from flight_agent.verification.store import ConversationStore
from tests.agent_helpers import RmseInput, analysis_plan_data
from tests.matlab_project_helpers import prepared_project


class ProbeInput(AgentContract):
    tag: str


class ReparseStat:
    def __init__(self, base):
        self._base = base
        self.st_file_attributes = 0x00000400

    def __getattr__(self, name):
        return getattr(self._base, name)


def project_executor(
    tmp_path,
    *,
    after_first=None,
    snapshot_root=None,
    secondary_state=None,
):
    source = prepared_project(tmp_path / "source")
    project = discover_matlab_project(str(source))
    resource = ResourceReference(
        id="local",
        kind="matlab_project",
        location="agent",
        locator=project.root,
        fingerprint=project.fingerprint,
        version="1",
        metadata={"project": project.model_dump(mode="json")},
    )
    verifications = []
    resources = ResourceRegistry()

    def verify(current):
        verifications.append(current.id)
        if discover_matlab_project(current.locator) != project or current != resource:
            raise ValueError("Source changed before confirmation")

    resources.register("matlab_project", "agent", verify, local=True)
    plan_resources = [resource.model_dump(mode="json")]
    requirements = [
        ResourceRequirement(
            role="project",
            kind="matlab_project",
            location="agent",
        )
    ]
    resource_bindings = {"project": "local"}
    if secondary_state is not None:
        secondary = ResourceReference(
            id="secondary",
            kind="calibration_data",
            location="worker",
            locator="worker://calibration/current",
            fingerprint="b" * 64,
            version="1",
        )

        def verify_secondary(current):
            verifications.append(current.id)
            if not secondary_state["valid"] or current != secondary:
                raise ValueError("Secondary resource changed before confirmation")

        resources.register(
            "calibration_data",
            "worker",
            verify_secondary,
        )
        plan_resources.append(secondary.model_dump(mode="json"))
        requirements.append(
            ResourceRequirement(
                role="secondary",
                kind="calibration_data",
                location="worker",
            )
        )
        resource_bindings["secondary"] = "secondary"
    store = ConversationStore(tmp_path / "state")
    snapshots = ProjectSnapshotStore(snapshot_root or store.root / "snapshots")
    calls = []

    def execute(context, inputs):
        binding = context.project_snapshot("project")
        tree = snapshots.verify(binding)
        calls.append(
            (
                context.call_id,
                binding.snapshot_id,
                (tree / "initialize.m").read_bytes(),
            )
        )
        if inputs.tag == "first" and after_first is not None:
            after_first(source)
        return CapabilityOutcome(
            findings=[
                Finding(
                    id="seen",
                    node_id=context.node_id,
                    text=inputs.tag,
                    status="observed",
                )
            ]
        )

    tools = CapabilityRegistry(agent_mode=True)
    tools.register(
        Capability(
            name="snapshot_probe",
            version="1",
            description="Read verified bound project bytes.",
            input_model=ProbeInput,
            output_model=CapabilityOutcome,
            effect="execute_project",
            operation=execute,
            scheduling=SchedulingMetadata(lane="local_matlab"),
            metadata=CapabilityMetadata(resources=requirements),
        )
    )
    executor = AgentExecutor(store, tools, resources, snapshots=snapshots)
    plan = AgentPlan.model_validate(
        {
            "title": "Read one confirmed immutable project",
            "rationale": "Two dependent registered calls use the same content.",
            "requirements": {
                "objective": "Read both findings without a compliance claim.",
                "mode": "analysis",
                "requested_outputs": [
                    {
                        "id": "first-output",
                        "kind": "finding",
                        "node_id": "first",
                        "item_id": "seen",
                        "description": "First finding",
                    },
                    {
                        "id": "second-output",
                        "kind": "finding",
                        "node_id": "second",
                        "item_id": "seen",
                        "description": "Second finding",
                    },
                ],
                "criteria": [],
                "overall_requested": False,
                "delivery_formats": [],
            },
            "resources": plan_resources,
            "nodes": [
                {
                    "id": "first",
                    "tool": "snapshot_probe",
                    "tool_version": "1",
                    "inputs": {"tag": "first"},
                    "resource_bindings": resource_bindings,
                },
                {
                    "id": "second",
                    "tool": "snapshot_probe",
                    "tool_version": "1",
                    "depends_on": ["first"],
                    "inputs": {"tag": "second"},
                    "resource_bindings": resource_bindings,
                },
            ],
            "allowed_effects": ["read_only", "execute_project"],
            "budget": {
                "max_rounds": 4,
                "max_calls": 4,
                "max_seconds": 60,
                "max_no_progress": 4,
            },
        }
    )
    return executor, store, source, plan, calls, verifications


def _queued_confirmation(store, pending):
    pending.queue_origin = ConversationState(
        status=pending.status,
        pending_decision=pending.pending_decision,
        error=pending.error,
    )
    pending.status = "queued"
    pending.queued_action = "confirm"
    pending.queued_action_committed = False
    store.save(pending)
    return store.get(pending.id)


def _assert_unconfirmed_without_references(store, conversation_id):
    restored = store.get(conversation_id)
    assert not restored.task.baseline.confirmed
    assert restored.task.baseline.project_snapshots == {}
    assert restored.task.baseline_sha256 is None
    assert restored.task.decisions == []
    with store._read() as connection:
        assert retained_snapshot_ids(connection) == set()
    return restored


def test_proposal_and_untrusted_confirmation_create_no_snapshot(tmp_path):
    executor, store, source, plan, calls, verifications = project_executor(tmp_path)
    pending = executor.propose(store.create(), plan)
    assert pending.task.baseline.project_snapshots == {}
    assert not executor.snapshots.root.exists()
    with pytest.raises(ValueError, match="consent"):
        executor.confirm(pending.id, pending.task.baseline.version, trusted=False)
    assert not executor.snapshots.root.exists()
    assert calls == []


def test_changed_source_requires_new_confirmation_without_dispatch(tmp_path):
    executor, store, source, plan, calls, verifications = project_executor(tmp_path)
    pending = executor.propose(store.create(), plan)
    (source / "initialize.m").write_text("new = 1;\n")
    with pytest.raises(ValueError, match="changed"):
        executor.confirm(pending.id, pending.task.baseline.version, trusted=True)
    restored = store.get(pending.id)
    assert not restored.task.baseline.confirmed
    assert restored.task.baseline.project_snapshots == {}
    assert restored.task.calls == []


def test_wrong_version_confirmation_rejects_before_snapshot_guard(tmp_path):
    executor, store, source, plan, calls, verifications = project_executor(tmp_path)
    pending = executor.propose(store.create(), plan)
    with pytest.raises(ValueError, match="exact current baseline"):
        executor.confirm(
            pending.id,
            pending.task.baseline.version + 1,
            trusted=True,
        )
    assert not executor.snapshots.root.exists()
    _assert_unconfirmed_without_references(store, pending.id)


def test_trusted_confirmation_persists_structurally_valid_binding(tmp_path):
    executor, store, source, plan, calls, verifications = project_executor(tmp_path)
    pending = executor.propose(store.create(), plan)
    confirmed = executor.confirm(
        pending.id,
        pending.task.baseline.version,
        trusted=True,
        _execute=False,
    )
    binding = confirmed.task.baseline.project_snapshots["local"]
    assert confirmed.task.baseline.confirmed
    assert executor.snapshots.verify(binding).is_dir()
    assert confirmed.task.baseline_sha256 is not None
    assert [decision.action for decision in confirmed.task.decisions] == [
        "confirm_baseline"
    ]
    with store._read() as connection:
        assert retained_snapshot_ids(connection) == {binding.snapshot_id}
    assert calls == []


def test_structural_binding_validation_rejects_unconfirmed_or_changed_binding(
    tmp_path,
):
    executor, store, source, plan, calls, verifications = project_executor(tmp_path)
    pending = executor.propose(store.create(), plan)
    confirmed = executor.confirm(
        pending.id,
        pending.task.baseline.version,
        trusted=True,
        _execute=False,
    )
    baseline = confirmed.task.baseline
    unconfirmed = baseline.model_copy(
        update={"confirmed": False, "confirmed_at": None}
    )
    with pytest.raises(ValueError, match="Unconfirmed"):
        executor._validate_structure(
            confirmed.task.model_copy(update={"baseline": unconfirmed})
        )

    binding = baseline.project_snapshots["local"].model_copy(
        update={"source_fingerprint": "f" * 64}
    )
    changed = baseline.model_copy(
        update={"project_snapshots": {"local": binding}}
    )
    with pytest.raises(ValueError, match="inventory|binding"):
        executor._validate_structure(
            confirmed.task.model_copy(update={"baseline": changed})
        )


def test_snapshot_root_overlap_with_source_rejects_proposal_without_creation(
    tmp_path,
):
    source_root = tmp_path / "source"
    executor, store, source, plan, calls, verifications = project_executor(
        tmp_path,
        snapshot_root=source_root / ".managed-snapshots",
    )
    with pytest.raises(ValueError, match="overlap"):
        executor.propose(store.create(), plan)
    assert not executor.snapshots.root.exists()


def test_empty_project_inventory_rejects_proposal_before_snapshot_guard(tmp_path):
    executor, store, source, plan, calls, verifications = project_executor(tmp_path)
    project = plan.resources[0].metadata["project"]
    empty = {
        **project,
        "files": [],
        "models": [],
        "entries": [],
        "project_files": [],
    }
    resource = plan.resources[0].model_copy(update={"metadata": {"project": empty}})
    invalid = plan.model_copy(update={"resources": [resource]})
    permissive = ResourceRegistry()
    permissive.register(
        "matlab_project",
        "agent",
        lambda current: None,
        local=True,
    )
    replacement = AgentExecutor(
        store,
        executor.tools,
        permissive,
        snapshots=executor.snapshots,
    )
    with pytest.raises(ValueError, match="inventory.*empty|empty.*inventory"):
        replacement.propose(store.create(), invalid)
    assert not executor.snapshots.root.exists()
    assert calls == []


def test_empty_project_inventory_rejects_confirmation_before_snapshot_guard(
    tmp_path,
):
    executor, store, source, plan, calls, verifications = project_executor(tmp_path)
    pending = executor.propose(store.create(), plan)
    record = store.get(pending.id)
    project = record.task.baseline.resources[0].metadata["project"]
    empty = {
        **project,
        "files": [],
        "models": [],
        "entries": [],
        "project_files": [],
    }
    resource = record.task.baseline.resources[0].model_copy(
        update={"metadata": {"project": empty}}
    )
    record.task.baseline = record.task.baseline.model_copy(
        update={"resources": [resource]}
    )
    store.save(record)
    with pytest.raises(ValueError, match="inventory.*empty|empty.*inventory"):
        executor.confirm(
            pending.id,
            pending.task.baseline.version,
            trusted=True,
            _execute=False,
        )
    assert not executor.snapshots.root.exists()
    _assert_unconfirmed_without_references(store, pending.id)
    assert calls == []


@pytest.mark.parametrize("boundary", ["managed", "source", "output"])
def test_reparse_boundary_component_rejects_before_snapshot_guard(
    tmp_path,
    monkeypatch,
    boundary,
):
    executor, store, source, plan, calls, verifications = project_executor(tmp_path)
    record = store.create()
    if boundary == "managed":
        executor.work_root.mkdir()
        target = executor.work_root
    elif boundary == "source":
        target = source
    else:
        target = tmp_path / "delivery"
        target.mkdir()
        record.output_root = str(target)
        store.save(record)
        plan = plan.model_copy(
            update={"allowed_effects": (*plan.allowed_effects, "write_delivery")}
        )
    original_lstat = Path.lstat

    def reparse_lstat(path):
        info = original_lstat(path)
        return ReparseStat(info) if path == target else info

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    with pytest.raises(ValueError, match="reparse"):
        executor.propose(record, plan)
    assert not executor.snapshots.root.exists()
    assert calls == []


def test_stale_managed_reservation_after_publication_persists_no_binding(
    tmp_path, monkeypatch
):
    executor, store, source, plan, calls, verifications = project_executor(tmp_path)
    pending = executor.propose(store.create(), plan)
    reservation = _queued_confirmation(store, pending)
    published = []
    ensure = executor.snapshots.ensure

    def publish_then_change_reservation(project):
        binding = ensure(project)
        published.append(binding)
        newer = store.get(reservation.id)
        newer.error = "newer queued reservation"
        store.save(newer)
        return binding

    monkeypatch.setattr(executor.snapshots, "ensure", publish_then_change_reservation)
    runtime = EngineeringRuntime(store, object(), executor, executor.resources)
    with pytest.raises(ValueError, match="queued decision is no longer current"):
        runtime.continue_decision(
            reservation.id,
            "confirm",
            version=reservation.task.baseline.version,
            trusted=True,
        )
    assert len(published) == 1
    assert executor.snapshots.verify(published[0]).is_dir()
    restored = _assert_unconfirmed_without_references(store, reservation.id)
    assert restored.error == "newer queued reservation"
    assert restored.queued_action == "confirm"
    assert restored.queued_action_committed is False
    assert calls == []


def test_lifecycle_guard_covers_managed_reservation_comparison_and_save(
    tmp_path, monkeypatch
):
    executor, store, source, plan, calls, verifications = project_executor(tmp_path)
    pending = executor.propose(store.create(), plan)
    reservation = _queued_confirmation(store, pending)
    state = {
        "held": False,
        "published": False,
        "compared": False,
        "saved": False,
    }
    guard = executor.snapshots.guard
    ensure = executor.snapshots.ensure
    get = store.get
    save = store.save

    @contextmanager
    def tracked_guard(*, exclusive=False):
        with guard(exclusive=exclusive):
            state["held"] = True
            try:
                yield
            finally:
                state["held"] = False

    def tracked_ensure(project):
        binding = ensure(project)
        state["published"] = True
        return binding

    def tracked_get(conversation_id):
        record = get(conversation_id)
        if state["published"] and not state["saved"]:
            assert state["held"]
            state["compared"] = True
        return record

    def tracked_save(record):
        if record.task is not None and record.task.baseline.confirmed:
            assert state["held"]
            save(record)
            state["saved"] = True
            return
        save(record)

    monkeypatch.setattr(executor.snapshots, "guard", tracked_guard)
    monkeypatch.setattr(executor.snapshots, "ensure", tracked_ensure)
    monkeypatch.setattr(store, "get", tracked_get)
    monkeypatch.setattr(store, "save", tracked_save)
    monkeypatch.setattr(executor, "_run", lambda *args, **kwargs: reservation)
    runtime = EngineeringRuntime(store, object(), executor, executor.resources)
    runtime.continue_decision(
        reservation.id,
        "confirm",
        version=reservation.task.baseline.version,
        trusted=True,
    )
    assert state == {
        "held": False,
        "published": True,
        "compared": True,
        "saved": True,
    }
    assert calls == []


def test_store_save_failure_after_publication_persists_no_binding(
    tmp_path, monkeypatch
):
    executor, store, source, plan, calls, verifications = project_executor(tmp_path)
    pending = executor.propose(store.create(), plan)
    published = []
    ensure = executor.snapshots.ensure
    save = store.save

    def capture_publication(project):
        binding = ensure(project)
        published.append(binding)
        return binding

    def fail_confirmed_save(record):
        if record.task is not None and record.task.baseline.confirmed:
            raise RuntimeError("injected confirmation save failure")
        save(record)

    monkeypatch.setattr(executor.snapshots, "ensure", capture_publication)
    monkeypatch.setattr(store, "save", fail_confirmed_save)
    with pytest.raises(RuntimeError, match="injected confirmation save failure"):
        executor.confirm(
            pending.id,
            pending.task.baseline.version,
            trusted=True,
            _execute=False,
        )
    assert len(published) == 1
    assert executor.snapshots.verify(published[0]).is_dir()
    _assert_unconfirmed_without_references(store, pending.id)
    assert calls == []


def test_post_copy_revalidates_all_original_resources(tmp_path, monkeypatch):
    secondary_state = {"valid": True}
    executor, store, source, plan, calls, verifications = project_executor(
        tmp_path,
        secondary_state=secondary_state,
    )
    pending = executor.propose(store.create(), plan)
    published = []
    ensure = executor.snapshots.ensure

    def publish_then_change_secondary(project):
        binding = ensure(project)
        published.append(binding)
        secondary_state["valid"] = False
        return binding

    monkeypatch.setattr(
        executor.snapshots,
        "ensure",
        publish_then_change_secondary,
    )
    with pytest.raises(ValueError, match="Secondary resource changed"):
        executor.confirm(
            pending.id,
            pending.task.baseline.version,
            trusted=True,
            _execute=False,
        )
    assert len(published) == 1
    assert executor.snapshots.verify(published[0]).is_dir()
    _assert_unconfirmed_without_references(store, pending.id)
    assert verifications.count("secondary") == 3
    assert verifications[-2:] == ["local", "secondary"]
    assert calls == []


def test_blocked_snapshot_resource_preflight_does_not_hold_sqlite_writer(
    tmp_path, monkeypatch
):
    executor, store, source, plan, calls, verifications = project_executor(tmp_path)
    pending = executor.propose(store.create(), plan)
    other = store.create()
    post_copy_entered = Event()
    release = Event()
    persisted = Event()
    errors = []
    verify = executor.resources.verify
    ensure = executor.snapshots.ensure
    verify_count = 0

    def capture_publication(project):
        binding = ensure(project)
        assert executor.snapshots.verify(binding).is_dir()
        return binding

    def blocking_verify(resources):
        nonlocal verify_count
        verify_count += 1
        verify(resources)
        if verify_count == 2:
            post_copy_entered.set()
            assert release.wait(5)

    def confirm():
        try:
            executor.confirm(
                pending.id,
                pending.task.baseline.version,
                trusted=True,
                _execute=False,
            )
        except BaseException as exc:
            errors.append(exc)

    def persist_other():
        try:
            record = store.get(other.id)
            record.status = "waiting_for_user"
            store.save(record)
        except BaseException as exc:
            errors.append(exc)
        finally:
            persisted.set()

    monkeypatch.setattr(executor.snapshots, "ensure", capture_publication)
    monkeypatch.setattr(executor.resources, "verify", blocking_verify)
    confirmer = Thread(target=confirm, daemon=True)
    writer = Thread(target=persist_other, daemon=True)
    try:
        confirmer.start()
        assert post_copy_entered.wait(5)
        writer.start()
        assert persisted.wait(1), (
            "an unrelated conversation writer was blocked by snapshot preflight"
        )
        assert not release.is_set()
        assert errors == []
    finally:
        release.set()
        confirmer.join(timeout=5)
        writer.join(timeout=5)
    assert not confirmer.is_alive()
    assert not writer.is_alive()
    assert errors == []
    assert store.get(other.id).status == "waiting_for_user"
    assert store.get(pending.id).task.baseline.confirmed
    assert calls == []


@pytest.mark.parametrize("resource_case", ["resource-free", "worker", "diagnostic"])
def test_nonlocal_direct_confirm_creates_no_snapshot_root(tmp_path, resource_case):
    tools = CapabilityRegistry(agent_mode=True)
    resources = ResourceRegistry()
    data = analysis_plan_data()
    requirements = []
    if resource_case != "resource-free":
        location = "worker" if resource_case == "worker" else "agent"
        kind = "worker_project" if resource_case == "worker" else "diagnostic_data"
        locator = (
            "worker://project"
            if resource_case == "worker"
            else str(tmp_path / "diagnostic" / "diagnostic.json")
        )
        if resource_case == "diagnostic":
            Path(locator).parent.mkdir()
            Path(locator).write_text("{}\n")
        resource = ResourceReference(
            id="input",
            kind=kind,
            location=location,
            locator=locator,
            fingerprint="a" * 64,
            version="1",
        )
        resources.register(
            kind,
            location,
            lambda current: None,
            local=resource_case == "diagnostic",
        )
        data["resources"] = [resource.model_dump(mode="json")]
        data["nodes"][0]["resource_bindings"] = {"input": "input"}
        requirements = [
            ResourceRequirement(role="input", kind=kind, location=location)
        ]

    def execute(context, inputs):
        return CapabilityOutcome()

    tools.register(
        Capability(
            name="offline_rmse",
            version="1",
            description="Confirm without a managed local MATLAB project.",
            input_model=RmseInput,
            output_model=CapabilityOutcome,
            effect="read_only",
            operation=execute,
            metadata=CapabilityMetadata(
                metrics=[MetricDefinition(id="rmse", label="RMSE", unit="m")],
                resources=requirements,
            ),
        )
    )
    store = ConversationStore(tmp_path / "state")
    executor = AgentExecutor(store, tools, resources)
    pending = executor.propose(store.create(), AgentPlan.model_validate(data))
    confirmed = executor.confirm(
        pending.id,
        pending.task.baseline.version,
        trusted=True,
        _execute=False,
    )
    assert confirmed.task.baseline.confirmed
    assert confirmed.task.baseline.project_snapshots == {}
    assert not executor.snapshots.root.exists()
