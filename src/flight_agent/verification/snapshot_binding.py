from pathlib import Path

from ..runtime.agent_contracts import AgentBaseline, AgentPlan
from ..runtime.project_snapshots import ProjectSnapshot, SnapshotFile
from ..runtime.resources import ResourceReference
from .intake import safe_directory, validate_output_root
from .matlab_contracts import MatlabProjectDescriptor
from .project_snapshots import ProjectSnapshotStore
from .resources import ResourceRegistry


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


def _overlap(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


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
