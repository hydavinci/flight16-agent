from pathlib import Path

from ..runtime.agent_contracts import AgentBaseline, AgentNode, AgentPlan
from ..runtime.project_snapshots import ProjectSnapshot, SnapshotFile
from ..runtime.resources import ResourceReference
from ..runtime.tasks import plan_ancestors
from .intake import safe_directory, validate_output_root
from .matlab_contracts import MatlabProjectDescriptor
from .project_snapshots import ProjectSnapshotStore
from .resources import ResourceRegistry
from .snapshot_lock import is_reparse_point


def local_projects(baseline: AgentPlan) -> dict[str, ResourceReference]:
    used = {
        identifier
        for node in baseline.nodes
        for identifier in node.resource_bindings.values()
    }
    return {
        resource.id: resource
        for resource in baseline.resources
        if resource.id in used
        and (resource.kind, resource.location) == ("matlab_project", "agent")
    }


def _descriptor(resource: ResourceReference) -> MatlabProjectDescriptor:
    project = MatlabProjectDescriptor.model_validate(resource.metadata.get("project"))
    if not project.files:
        raise ValueError("Project inventory cannot be empty")
    if resource.version != "1" or (
        project.root,
        project.fingerprint,
    ) != (resource.locator, resource.fingerprint):
        raise ValueError("Project descriptor differs from the confirmed resource")
    return project


def validate_snapshot_bindings(
    baseline: AgentBaseline,
    *,
    require: bool,
) -> None:
    expected = local_projects(baseline)
    projects = {
        identifier: _descriptor(resource)
        for identifier, resource in expected.items()
    }
    bindings = baseline.project_snapshots
    if bindings and not baseline.confirmed:
        raise ValueError("Unconfirmed baseline cannot contain managed snapshot bindings")
    if (require or bindings) and set(bindings) != set(expected):
        raise ValueError("Local execution requires a newly confirmed snapshot baseline")
    for identifier, binding in bindings.items():
        resource = expected[identifier]
        project = projects[identifier]
        manifest_files = [
            SnapshotFile.model_validate(item.model_dump(mode="json"))
            for item in project.files
        ]
        if (
            binding.source_fingerprint != resource.fingerprint
            or binding.manifest.files != manifest_files
        ):
            raise ValueError(
                "Snapshot binding differs from the confirmed project inventory"
            )
        ProjectSnapshot.model_validate(binding.model_dump(mode="json"))


def prepare_project_snapshots(
    baseline: AgentBaseline,
    snapshots: ProjectSnapshotStore,
) -> dict[str, ProjectSnapshot]:
    return {
        identifier: snapshots.ensure(_descriptor(resource))
        for identifier, resource in sorted(local_projects(baseline).items())
    }


def snapshot_lineage(
    baseline: AgentBaseline,
    node: AgentNode,
) -> dict[str, ProjectSnapshot]:
    producers = {node.id, *plan_ancestors(baseline.nodes)[node.id]}
    resource_ids = {
        identifier
        for candidate in baseline.nodes
        if candidate.id in producers
        for identifier in candidate.resource_bindings.values()
    }
    return {
        identifier: binding
        for identifier, binding in sorted(baseline.project_snapshots.items())
        if identifier in resource_ids
    }


def verify_snapshot_lineage(
    baseline: AgentBaseline,
    node: AgentNode,
    snapshots: ProjectSnapshotStore,
) -> None:
    validate_snapshot_bindings(
        baseline,
        require=bool(baseline.project_snapshots),
    )
    bindings = snapshot_lineage(baseline, node)
    if bindings:
        with snapshots.guard():
            for binding in bindings.values():
                snapshots.verify(binding)


def verify_execution_resources(
    baseline: AgentBaseline,
    registry: ResourceRegistry,
) -> None:
    registry.verify(
        [
            resource
            for resource in baseline.resources
            if resource.id not in baseline.project_snapshots
        ]
    )


def _overlap(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def _reject_reparse_components(path: Path) -> None:
    lexical = path if path.is_absolute() else path.absolute()
    current = Path(lexical.anchor)
    components = [current]
    for part in lexical.parts[1:]:
        current /= part
        components.append(current)
    for component in components:
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if is_reparse_point(info):
            raise ValueError(
                f"Managed boundary path cannot traverse a reparse point: {component}"
            )


def validate_managed_boundaries(
    baseline: AgentPlan,
    registry: ResourceRegistry,
    snapshots: ProjectSnapshotStore,
    work_root: Path,
    output: str | None,
) -> None:
    normalized_output = validate_output_root(output)
    safe_directory(work_root)
    managed = [snapshots.root, work_root]
    if _overlap(managed[0], managed[1]):
        raise ValueError("Snapshot and mutable work roots must be separate")
    source_roots = (
        [
            Path(resource.locator)
            for resource in baseline.resources
            if resource.location == "agent"
        ]
        if isinstance(baseline, AgentBaseline) and baseline.confirmed
        else registry.local_roots(baseline.resources)
    )
    boundary_paths = [
        *managed,
        *source_roots,
        *([Path(normalized_output)] if normalized_output else []),
    ]
    for path in boundary_paths:
        _reject_reparse_components(path)
    protected = [
        *source_roots,
        *([Path(normalized_output)] if normalized_output else []),
    ]
    for owned in managed:
        for external in protected:
            if _overlap(owned, external):
                raise ValueError(
                    "Managed snapshot/work storage overlaps an external resource"
                )
    if normalized_output:
        for source in source_roots:
            if _overlap(Path(normalized_output), source):
                raise ValueError("Output directory must not overlap the source project")
