from pathlib import Path

from flight_agent.runtime.project_snapshots import SnapshotFile, SnapshotManifest
from flight_agent.verification.matlab_projects import discover_matlab_project
from tests.matlab_project_helpers import prepared_project


def manifest_for(project):
    return SnapshotManifest(
        files=[
            SnapshotFile.model_validate(item.model_dump(mode="json"))
            for item in project.files
        ]
    )


def snapshot_case(tmp_path: Path):
    from flight_agent.verification.project_snapshots import ProjectSnapshotStore

    source = prepared_project(tmp_path / "source")
    project = discover_matlab_project(str(source))
    snapshots = ProjectSnapshotStore(tmp_path / "managed" / "snapshots")
    with snapshots.guard():
        binding = snapshots.ensure(project)
    return source, project, snapshots, binding


def bind_request(request, snapshots):
    if request.project is None:
        return request
    with snapshots.guard():
        binding = snapshots.ensure(request.project)
    return type(request).model_validate({
        **request.model_dump(mode="json"),
        "project_snapshot": binding.model_dump(mode="json"),
    })
