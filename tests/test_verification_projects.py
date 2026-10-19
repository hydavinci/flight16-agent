import json

import pytest

from tests.verification_helpers import project_tree, verification_module


@pytest.fixture
def prepared_project(tmp_path):
    return project_tree(tmp_path / "project with spaces")


def test_folder_discovery_reads_registered_entries_without_execution(prepared_project):
    projects = verification_module("projects")
    project = projects.ProjectInspector().inspect(str(prepared_project))
    assert project.root == str(prepared_project.resolve())
    assert project.config.model == "model.slx"
    assert project.config.initializer == "initialize.m"
    assert project.config.response_signal.name == "response"
    assert {item.path for item in project.files} == {
        "model.slx",
        "initialize.m",
        "data/input.mat",
        "flight-agent.project.json",
    }
    assert all(len(item.sha256) == 64 for item in project.files)


def test_project_fingerprint_changes_with_dependency(prepared_project):
    inspector = verification_module("projects").ProjectInspector()
    first = inspector.inspect(str(prepared_project))
    (prepared_project / "data" / "input.mat").write_bytes(b"new data")
    second = inspector.inspect(str(prepared_project))
    assert second.fingerprint != first.fingerprint


def test_discovery_requires_directory_not_individual_file(prepared_project):
    inspector = verification_module("projects").ProjectInspector()
    with pytest.raises(ValueError, match="directory"):
        inspector.inspect(str(prepared_project / "model.slx"))


def test_unregistered_project_reports_candidates_instead_of_guessing(prepared_project):
    inspector = verification_module("projects").ProjectInspector()
    (prepared_project / "flight-agent.project.json").unlink()
    (prepared_project / "other.slx").write_bytes(b"another model")
    with pytest.raises(ValueError, match="flight-agent.project.json") as error:
        inspector.inspect(str(prepared_project))
    assert "model.slx" in str(error.value) and "other.slx" in str(error.value)


def test_discovery_reports_missing_dependency(prepared_project):
    inspector = verification_module("projects").ProjectInspector()
    (prepared_project / "data" / "input.mat").unlink()
    with pytest.raises(ValueError, match="data/input.mat"):
        inspector.inspect(str(prepared_project))


@pytest.mark.parametrize("field", ["model", "initializer", "dependencies"])
def test_manifest_cannot_escape_project(prepared_project, field):
    inspector = verification_module("projects").ProjectInspector()
    path = prepared_project / "flight-agent.project.json"
    data = json.loads(path.read_text())
    data[field] = ["../outside.mat"] if field == "dependencies" else "../outside.m"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="relative|inside|traversal"):
        inspector.inspect(str(prepared_project))


def test_external_symlink_is_not_followed(prepared_project, tmp_path):
    inspector = verification_module("projects").ProjectInspector()
    outside = tmp_path / "private.txt"
    outside.write_text("must not be read")
    (prepared_project / "external.txt").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        inspector.inspect(str(prepared_project))


def test_snapshot_copies_complete_package_without_touching_source(prepared_project, tmp_path):
    projects = verification_module("projects")
    project = projects.ProjectInspector().inspect(str(prepared_project))
    snapshot = projects.snapshot_project(project, tmp_path / "work" / "project")
    assert (snapshot / "data" / "input.mat").read_bytes() == b"registered dependency"
    (snapshot / "model.slx").write_bytes(b"runtime-only change")
    assert (prepared_project / "model.slx").read_bytes() != b"runtime-only change"
    assert projects.ProjectInspector().inspect(str(prepared_project)).fingerprint == (
        project.fingerprint
    )


def test_snapshot_rejects_source_changes_after_confirmation(prepared_project, tmp_path):
    projects = verification_module("projects")
    project = projects.ProjectInspector().inspect(str(prepared_project))
    (prepared_project / "initialize.m").write_text("changed source")
    with pytest.raises(ValueError, match="changed|fingerprint"):
        projects.snapshot_project(project, tmp_path / "work")


def test_snapshot_cannot_be_inside_source(prepared_project):
    projects = verification_module("projects")
    project = projects.ProjectInspector().inspect(str(prepared_project))
    with pytest.raises(ValueError, match="outside"):
        projects.snapshot_project(project, prepared_project / "output")


def test_f16_package_profile_uses_explicit_trim_and_registered_signals(tmp_path):
    projects = verification_module("projects")
    root = tmp_path / "F16_package"
    paths = [
        "F16_CAS_Gain/F16BLOCK_ROBUST_SURFACE_CONTROL.slx",
        "F16_CAS_Gain/initialize_F16_ROBUST_SURFACE_workspace.m",
        "F16_CAS_Gain/select_F16_trim.m",
        "F16_CAS_Gain/data/F16TrimGrid.mat",
        "F16_CAS_Gain/data/F16GainSurface.mat",
        "F16_CAS_Gain/data/F16ControlEffectiveness.mat",
        "F16Sim/nlplant.c",
        "F16Sim/nlplant.mexw64",
        "F16Sim/F16_Actuator_Library.mdl",
    ]
    for relative in paths:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"discovery-only fixture")
    project = projects.ProjectInspector().inspect(str(root))
    assert project.config.trim_selector == "F16_CAS_Gain/select_F16_trim.m"
    assert project.config.trim_selection_variable == "F16CasInitialConditionSelection"
    assert project.config.supported_trim_points == [1]
    assert project.config.input_signal.name == "stick_lb"
    assert project.config.response_signal.name == "q_deg_s"
    assert project.config.native_modules[0].name == "nlplant"


def test_unknown_backend_is_explicitly_unsupported(prepared_project):
    inspector = verification_module("projects").ProjectInspector()
    path = prepared_project / "flight-agent.project.json"
    data = json.loads(path.read_text())
    data["backend"] = "simate"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="unsupported|Unsupported"):
        inspector.inspect(str(prepared_project))
