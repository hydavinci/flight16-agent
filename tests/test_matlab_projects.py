import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest
from pydantic import ValidationError

from tests.matlab_project_helpers import prepared_project
from tests.verification_helpers import verification_module


def discover(root):
    return verification_module("matlab_projects").discover_matlab_project(str(root))


def test_project_without_manifest_is_discovered_without_execution(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery must not execute a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    root = prepared_project(tmp_path / "case", model_name="pitch")
    project = discover(root)
    assert project.root == str(root)
    assert project.host == "agent"
    assert project.models == ["pitch.slx"]
    assert [(entry.path, entry.kind) for entry in project.entries] == [("initialize.m", "script")]
    assert project.entries[0].arguments == []
    assert project.entries[0].evidence == ["initialize.m:2"]
    assert len(project.fingerprint) == 64
    assert {item.path for item in project.files} == {
        "initialize.m",
        "pitch.slx",
        "README.md",
        "data/trim.mat",
    }
    assert project.settings["matlab_literals"] == [
        {"path": "initialize.m", "location": "initialize.m:2", "name": "StopTime", "value": 12},
        {
            "path": "initialize.m",
            "location": "initialize.m:3",
            "name": "condition",
            "value": "cruise",
        },
    ]
    assert {
        "path": "pitch.slx",
        "location": "simulink/configSet0.xml:StopTime",
        "name": "StopTime",
        "value": "12",
    } in project.settings["model_parameters"]
    assert project.settings["documentation"][0]["path"] == "README.md"
    assert "Response signal: response" in project.settings["documentation"][0]["text"]
    assert "model" not in project.settings  # Candidates are not execution selections.


def test_second_layout_preserves_all_entries_models_and_project_files(tmp_path):
    root = tmp_path / "another layout"
    prepared_project(root / "simulation", model_name="roll", signal_name="rate")
    (root / "analysis").mkdir()
    (root / "analysis" / "measure.m").write_text(
        "% analysis\n%{\nfunction misleading\n%}\n"
        "function [series, info] = measure( ...\n    condition, options)\nend\n"
    )
    (root / "legacy.mdl").write_text('Model {\n StopTime "8"\n Solver "ode45"\n}\n')
    (root / "project.prj").write_text(
        "<project><dependency>toolbox</dependency><startup>simulation/initialize.m</startup>"
        "</project>"
    )
    (root / "other.slx").write_bytes(b"opaque static model")
    project = discover(root)
    assert project.models == ["legacy.mdl", "other.slx", "simulation/roll.slx"]
    assert project.project_files == ["project.prj"]
    assert [entry.path for entry in project.entries] == [
        "analysis/measure.m",
        "simulation/initialize.m",
    ]
    entry = project.entries[0]
    assert entry.kind == "function"
    assert entry.signature == "function [series, info] = measure( condition, options)"
    assert entry.arguments == []
    assert entry.evidence == ["analysis/measure.m:5"]
    assert any(item["value"] == "ode45" for item in project.settings["model_parameters"])
    assert any(item["value"] == "toolbox" for item in project.settings["project_metadata"])
    assert any("other.slx" in note for note in project.notes)


def test_matlab_only_local_functions_and_unsupported_signatures(tmp_path):
    root = tmp_path / "matlab only"
    root.mkdir()
    (root / "analyze.m").write_text("function result = analyze(params)\nend\n")
    (root / "script.m").write_text("x = 1;\nfunction helper\nend\n")
    (root / "Thing.m").write_text("classdef Thing\nend\n")
    project = discover(root)
    assert project.models == []
    assert [(entry.path, entry.kind, entry.signature) for entry in project.entries] == [
        ("analyze.m", "function", "function result = analyze(params)"),
        ("script.m", "script", None),
    ]
    assert any("Thing.m" in note and "classdef" in note for note in project.notes)


@pytest.mark.parametrize(
    "parameters,body,counts",
    [
        ("folder", "if nargin == 0\nfolder = fileparts(mfilename('fullpath'));\nend\n", [0]),
        ("a,b", "if nargin < 2\nb = 2;\nend\nif nargin < 1\na = 1;\nend\n", [0, 1]),
        ("a,b", "if nargin == 0\na = 1;\nb = 2;\nend\n", [0]),
        ("a,b", "if nargin < 2\nb = a + 1;\nend\n", [1]),
        ("folder", "if nargin == 0\nfolder = 'a;b,c';\nend\n", [0]),
    ],
)
def test_leading_nargin_defaults_are_source_backed(tmp_path, parameters, body, counts):
    (tmp_path / "initialize.m").write_text(
        f"function [point, selection] = initialize({parameters})\n"
        + body
        + "point = 1;\nselection = 2;\nend\n"
    )
    project = discover(tmp_path)
    assert project.settings["function_defaults"] == [
        {
            "path": "initialize.m",
            "location": "initialize.m:2",
            "name": "argument_counts",
            "value": counts,
        }
    ]


@pytest.mark.parametrize(
    "body",
    [
        "% if nargin == 0\n% folder = pwd;\n% end\n",
        "message = 'if nargin == 0';\nfolder = pwd;\n",
        "if nargin == 0\nother = pwd;\nend\n",
        "if nargin == 0\nfolder = folder;\nend\n",
        "if nargin == 0\nif ready\nfolder = pwd;\nend\nend\n",
        "if nargin == 0\nfolder = pwd;\nelse\nfolder = pwd;\nend\n",
        "use(folder);\nif nargin == 0\nfolder = pwd;\nend\n",
        "if nargin == 0\nfolder == 1;\nend\n",
        "if nargin == 0\nfolder = 1; clear folder;\nend\n",
        "if nargin == 0\nfolder = 1, clear folder;\nend\n",
        "if nargin == 0\nfolder = 1; clear;\nend\n",
        "if nargin == 0\nfolder = 1, clear;\nend\n",
    ],
)
def test_unproven_optional_inputs_are_not_invented(tmp_path, body):
    (tmp_path / "initialize.m").write_text(
        "function result = initialize(folder)\n" + body + "result = 1;\nend\n"
    )
    assert discover(tmp_path).settings.get("function_defaults", []) == []


@pytest.mark.parametrize(
    "signature,parameter",
    [
        ("function result = initialize(nargin)", "nargin"),
        ("function nargin = initialize(folder)", "folder"),
    ],
)
def test_shadowed_nargin_is_not_optional_input_evidence(tmp_path, signature, parameter):
    (tmp_path / "initialize.m").write_text(
        signature + f"\nif nargin == 0\n{parameter} = 1;\nend\nend\n"
    )
    assert discover(tmp_path).settings.get("function_defaults", []) == []


def test_script_does_not_declare_optional_inputs(tmp_path):
    (tmp_path / "initialize.m").write_text(
        "function_call(folder)\nif nargin == 0\nfolder = 1;\nend\n"
    )
    assert discover(tmp_path).settings.get("function_defaults", []) == []


def test_optional_input_evidence_respects_serialized_metadata_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(verification_module("matlab_projects"), "MAX_RESULT", 100)
    (tmp_path / "initialize.m").write_text(
        "function result = initialize(folder)\n"
        "if nargin == 0\nfolder = pwd;\nend\nresult = 1;\nend\n"
    )
    project = discover(tmp_path)
    assert len(json.dumps(project.settings, ensure_ascii=False).encode("utf-8")) <= 100
    assert project.settings.get("function_defaults", []) == []
    assert [entry.path for entry in project.entries] == ["initialize.m"]


def test_static_literals_are_not_expressions_or_commented_code(tmp_path):
    root = tmp_path / "literal only"
    root.mkdir()
    (root / "initialize.m").write_text(
        "% hidden = 1;\n%{\nblocked = 2;\n%}\n"
        "rate = 3.5;\nlabel = 'it''s explicit'; % trailing comment\n"
        "enabled = true;\nvector = [1, 2];\n"
        "unsafe = system('touch must-not-exist');\n"
        "result = 1 + 2;\nfunction local\nrate = 99;\nend\n"
    )
    project = discover(root)
    values = {item["name"]: item["value"] for item in project.settings["matlab_literals"]}
    assert values == {"rate": 3.5, "label": "it's explicit", "enabled": True, "vector": [1, 2]}
    assert any("unresolved" in note.lower() and "initialize.m:10" in note for note in project.notes)
    assert not (root / "must-not-exist").exists()


def test_conflicting_static_values_keep_both_sources(tmp_path):
    root = prepared_project(tmp_path / "case")
    (root / "alternative.m").write_text("StopTime = 25;\n")
    project = discover(root)
    literals = project.settings["matlab_literals"]
    assert {(item["path"], item["value"]) for item in literals if item["name"] == "StopTime"} == {
        ("alternative.m", 25),
        ("initialize.m", 12),
    }
    assert any("conflict" in note.lower() and "StopTime" in note for note in project.notes)


def test_conflicting_model_parameters_are_not_silently_selected(tmp_path):
    root = prepared_project(tmp_path / "case")
    (root / "other.mdl").write_text('Model {\n StopTime "25"\n}\n')
    project = discover(root)
    parameters = project.settings["model_parameters"]
    assert {(item["path"], item["value"]) for item in parameters if item["name"] == "StopTime"} == {
        ("other.mdl", "25"),
        ("plant.slx", "12"),
    }
    assert any("conflict" in note.lower() and "StopTime" in note for note in project.notes)


def test_discovery_retains_candidates_even_if_names_need_execution_validation(tmp_path):
    root = prepared_project(tmp_path / "case")
    (root / "old-model.mdl").write_text("Model {\n}\n")
    (root / "analysis draft.m").write_text("function result = analysis(params)\nend\n")
    project = discover(root)
    assert project.models == ["old-model.mdl", "plant.slx"]
    assert [(entry.path, entry.kind) for entry in project.entries] == [
        ("analysis draft.m", "function"),
        ("initialize.m", "script"),
    ]
    contracts = verification_module("matlab_contracts")
    with pytest.raises(ValidationError, match="identifier"):
        contracts.ProjectExecutionInput(mode="entry", entry=project.entries[0])


def test_json_only_syntax_is_not_claimed_as_a_matlab_literal(tmp_path):
    root = tmp_path / "case"
    root.mkdir()
    (root / "initialize.m").write_text(
        'not_matlab = [{"value": 1}];\nnot_matlab_either = [null];\n'
    )
    project = discover(root)
    assert project.settings["matlab_literals"] == []
    assert sum("unresolved assignment" in note for note in project.notes) == 2


def test_large_statements_keep_entry_candidates_without_claiming_a_supported_signature(tmp_path):
    root = tmp_path / "case"
    root.mkdir()
    (root / "analyze.m").write_text("function result = analyze(" + "x," * 3000 + "y)\nend\n")
    (root / "script.m").write_text("text = '" + "x" * 5000 + "';\n")
    project = discover(root)
    assert [(entry.path, entry.kind, entry.signature) for entry in project.entries] == [
        ("analyze.m", "function", None),
        ("script.m", "script", None),
    ]
    assert any("analyze.m" in note and "signature" in note for note in project.notes)


def test_generic_discovery_does_not_use_legacy_profile_inference(tmp_path, monkeypatch):
    inventory = verification_module("projects")

    def forbidden():
        raise AssertionError("Generic discovery must not infer a legacy F-16 profile")

    monkeypatch.setattr(inventory, "_f16_profile", forbidden)
    root = prepared_project(tmp_path / "case")
    assert discover(root).models == ["plant.slx"]


@pytest.mark.parametrize("kind", ["relative", "file", "missing", "traversal", "control"])
def test_invalid_project_roots_are_rejected(tmp_path, kind):
    root = prepared_project(tmp_path / "case")
    value = {
        "relative": Path("case"),
        "file": root / "initialize.m",
        "missing": tmp_path / "missing",
        "traversal": str(root) + "/../case",
        "control": str(root) + "\n",
    }[kind]
    with pytest.raises(ValueError, match="directory|traversal|control"):
        discover(value)


@pytest.mark.parametrize("kind", ["root", "ancestor", "file", "directory", "broken"])
def test_discovery_rejects_symlink_traversal(tmp_path, kind):
    root = prepared_project(tmp_path / "case")
    if kind in {"root", "ancestor"}:
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        path = alias if kind == "root" else alias / "data"
    else:
        target = root / (
            "missing" if kind == "broken" else "data" if kind == "directory" else "initialize.m"
        )
        (root / "link").symlink_to(target)
        path = root
    with pytest.raises(ValueError, match="symlink"):
        discover(path)


@pytest.mark.parametrize("limit", ["MAX_PROJECT_FILES", "MAX_PROJECT_BYTES"])
def test_discovery_reuses_existing_snapshot_limits(tmp_path, monkeypatch, limit):
    inventory = verification_module("projects")
    root = prepared_project(tmp_path / "case")
    monkeypatch.setattr(inventory, limit, 1)
    with pytest.raises(ValueError, match="snapshot limit"):
        discover(root)


def test_metadata_bounds_and_archives_do_not_extract_or_expand_unboundedly(tmp_path):
    projects = verification_module("matlab_projects")
    root = prepared_project(tmp_path / "case")
    with ZipFile(root / "oversized.slx", "w") as archive:
        archive.writestr("../../escape.xml", '<P Name="StopTime">66</P>')
        archive.writestr("simulink/big.xml", "x" * (projects.MAX_METADATA_BYTES + 1))
        archive.writestr(
            "simulink/entity.xml", '<!DOCTYPE x [<!ENTITY x "bad">]><P Name="StopTime">&x;</P>'
        )
    (root / "README-large.md").write_text("z" * (projects.MAX_METADATA_BYTES + 1))
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    project = discover(root)
    assert project.models == ["oversized.slx", "plant.slx"]
    assert any("limit" in note.lower() for note in project.notes)
    assert any("XML" in note for note in project.notes)
    assert not any(item["value"] == "66" for item in project.settings["model_parameters"])
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert not (tmp_path / "escape.xml").exists()


@pytest.mark.parametrize("assignments", [1000, 60_000])
def test_expanded_metadata_is_truncated_without_losing_entry_or_file_identity(
    tmp_path, assignments
):
    root = tmp_path / "case"
    root.mkdir()
    script = root / "initialize.m"
    source = "x = 1;\n" * assignments
    script.write_text(source, encoding="utf-8")
    project = discover(root)
    assert [(entry.path, entry.kind) for entry in project.entries] == [("initialize.m", "script")]
    assert [(item.path, item.size, item.sha256) for item in project.files] == [
        ("initialize.m", len(source), hashlib.sha256(source.encode()).hexdigest())
    ]
    literals = project.settings["matlab_literals"]
    assert 0 < len(literals) <= assignments
    assert len(json.dumps(project.settings, ensure_ascii=False).encode("utf-8")) <= 4_000_000
    if assignments == 1000:
        assert len(literals) == assignments
        assert not any("truncated" in note.lower() for note in project.notes)
    else:
        assert len(literals) < assignments
        assert any(
            "truncated" in note.lower() and "unresolved" in note.lower() for note in project.notes
        )
        # A change outside the retained prefix must still change resource identity.
        script.write_text(source[:-7] + "x = 2;\n", encoding="utf-8")
        changed = discover(root)
        assert changed.settings == project.settings
        assert changed.fingerprint != project.fingerprint


@pytest.mark.parametrize("source_kind", ["mdl", "slx", "prj", "readme"])
def test_serialized_metadata_budget_is_shared_across_sources_and_long_paths(tmp_path, source_kind):
    root = tmp_path / "case"
    root.mkdir()
    (root / "initialize.m").write_text("x = 1;\n" * 30_000, encoding="utf-8")
    nested = root.joinpath(*(['资源"' + "n" * 90] * 3))
    nested.mkdir(parents=True)
    if source_kind == "slx":
        with ZipFile(nested / "a_model.slx", "w") as archive:
            archive.writestr(
                "simulink/config.xml",
                "<ConfigSet>" + '<P Name="StopTime">1</P>' * 4000 + "</ConfigSet>",
            )
    else:
        name, content = {
            "mdl": ("a_model.mdl", "Model {\n" + ' StopTime "1"\n' * 4000 + "}\n"),
            "prj": (
                "a_project.prj",
                "<project>" + "<dependency>x</dependency>" * 4000 + "</project>",
            ),
            "readme": ("README.md", "\x01" * 250_000),
        }[source_kind]
        (nested / name).write_text(content, encoding="utf-8")
    (nested / "zz_analyze.m").write_text("function result = zz_analyze\nend\n", encoding="utf-8")
    (nested / "zz_model.mdl").write_text("Model {\n}\n", encoding="utf-8")
    before = {
        p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    project = discover(root)
    assert {item.path for item in project.files} == set(before)
    assert all(
        item.sha256 == hashlib.sha256(before[item.path]).hexdigest() for item in project.files
    )
    assert {entry.path for entry in project.entries} == {
        "initialize.m",
        (nested / "zz_analyze.m").relative_to(root).as_posix(),
    }
    assert (nested / "zz_model.mdl").relative_to(root).as_posix() in project.models
    assert len(project.settings["matlab_literals"]) == 30_000
    if source_kind == "readme":
        assert project.settings["documentation"] == []
    else:
        group = "project_metadata" if source_kind == "prj" else "model_parameters"
        assert 0 < len(project.settings[group]) < 4000
        candidates = project.project_files if source_kind == "prj" else project.models
        assert (
            nested / ("a_project.prj" if source_kind == "prj" else f"a_model.{source_kind}")
        ).relative_to(root).as_posix() in candidates
    assert len(json.dumps(project.settings, ensure_ascii=False).encode("utf-8")) <= 4_000_000
    assert any(
        "truncated" in note.lower() and "unresolved" in note.lower() for note in project.notes
    )
    assert {
        p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()
    } == before


def test_fingerprint_binds_complete_inventory_and_settings(tmp_path):
    projects = verification_module("matlab_projects")
    from flight_agent.runtime.contracts import fingerprint

    root = prepared_project(tmp_path / "case")
    first = discover(root)
    expected = fingerprint(
        {
            "files": [item.model_dump(mode="json") for item in first.files],
            "settings": first.settings,
        }
    )
    assert first.fingerprint == expected
    assert discover(root).model_dump() == first.model_dump()
    (root / "data" / "trim.mat").write_bytes(b"changed binary resource")
    assert discover(root).fingerprint != first.fingerprint
    changed = first.model_copy(update={"settings": {"selected": "unconfirmed"}})
    with pytest.raises(ValueError, match="changed|fingerprint"):
        projects.snapshot_matlab_project(changed, tmp_path / "copy")


def test_snapshot_is_complete_isolated_and_source_preserving(tmp_path):
    projects = verification_module("matlab_projects")
    root = prepared_project(tmp_path / "case")
    project = discover(root)
    copied = projects.snapshot_matlab_project(project, tmp_path / "runtime" / "project")
    assert copied == tmp_path / "runtime" / "project"
    assert not any(path.is_symlink() for path in copied.rglob("*"))
    assert {p.relative_to(copied).as_posix() for p in copied.rglob("*") if p.is_file()} == {
        item.path for item in project.files
    }
    for item in project.files:
        assert hashlib.sha256((copied / item.path).read_bytes()).hexdigest() == item.sha256
    (copied / "initialize.m").write_text("runtime changes only")
    assert discover(root) == project


@pytest.mark.parametrize("change", ["edit", "add", "delete", "symlink", "directory-symlink"])
def test_snapshot_checks_full_source_before_and_after_copy(tmp_path, monkeypatch, change):
    projects = verification_module("matlab_projects")
    root = prepared_project(tmp_path / "case")
    project = discover(root)
    original_copy = shutil.copyfile
    changed = False

    def copy_and_mutate(source, target):
        nonlocal changed
        result = original_copy(source, target)
        if not changed:
            changed = True
            if change == "add":
                (root / "new.bin").write_bytes(b"new")
            elif change == "delete":
                (root / "README.md").unlink()
            elif change in {"symlink", "directory-symlink"}:
                path = root / ("data" if change == "directory-symlink" else "README.md")
                moved = tmp_path / "moved"
                path.rename(moved)
                path.symlink_to(moved)
            else:
                (root / "README.md").write_text("changed during copy")
        return result

    monkeypatch.setattr(shutil, "copyfile", copy_and_mutate)
    with pytest.raises(ValueError, match="changed|symlink"):
        projects.snapshot_matlab_project(project, tmp_path / "copy")


@pytest.mark.parametrize("where", ["before", "inside", "same", "ancestor", "alias", "exists"])
def test_snapshot_rejects_changed_sources_and_unsafe_destinations(tmp_path, where):
    projects = verification_module("matlab_projects")
    root = prepared_project(tmp_path / "case")
    project = discover(root)
    target = tmp_path / "copy"
    if where == "before":
        (root / "data" / "trim.mat").write_bytes(b"not confirmed")
    elif where == "inside":
        target = root / "output"
    elif where == "same":
        target = root
    elif where == "ancestor":
        target = tmp_path
    elif where == "alias":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        target = alias / "copy"
    else:
        target.mkdir()
    with pytest.raises((ValueError, FileExistsError)):
        projects.snapshot_matlab_project(project, target)
    assert not (root / "output").exists()


def test_entry_and_signal_contracts_reject_expressions_and_unbounded_json():
    contracts = verification_module("matlab_contracts")
    entry = contracts.MatlabEntry(path="analysis/run.m", kind="function", arguments=[{"x": 1}])
    assert contracts.MatlabEntry.model_validate_json(entry.model_dump_json()) == entry
    for path in ("../run.m", "/run.m", "run.m\n", "run.txt"):
        with pytest.raises(ValidationError):
            contracts.MatlabEntry(path=path, kind="script")
    for arguments in ([float("nan")], [float("inf")], ["x" * 100_001]):
        with pytest.raises(ValidationError):
            contracts.MatlabEntry(path="run.m", kind="function", arguments=arguments)
    with pytest.raises(ValidationError, match="script"):
        contracts.MatlabEntry(path="run.m", kind="script", arguments=[1])
    for path in ([], ["x(1)"], ["x.y"], ["../x"], ["x;system"], ["x" * 64]):
        with pytest.raises(ValidationError):
            contracts.SignalSelection(source="workspace", path=path, unit="deg")
    selection = contracts.SignalSelection(source="logsout", path=["response", "Values"], unit="deg")
    assert selection.path == ["response", "Values"]


def test_execution_contract_is_explicit_and_signal_pair_is_optional():
    contracts = verification_module("matlab_contracts")
    generic = contracts.ProjectExecutionInput(
        mode="entry", entry={"path": "analyze.m", "kind": "function"}
    )
    assert generic.model is None and generic.initializer is None
    assert generic.input_signal is None and generic.response_signal is None
    assert generic.working_directory is None and generic.conditions == {}
    selection = {"source": "return", "path": ["response"], "unit": "deg"}
    paired = contracts.ProjectExecutionInput(
        mode="simulink",
        model="nested/plant.mdl",
        input_signal=selection,
        response_signal=selection,
    )
    assert paired.entry is None and paired.project_file is None
    bad = [
        {"mode": "entry"},
        {"mode": "simulink"},
        {"mode": "entry", "entry": generic.entry, "model": "plant.slx"},
        {"mode": "simulink", "model": "plant.slx", "entry": generic.entry},
        {"mode": "entry", "entry": generic.entry, "input_signal": selection},
        {"mode": "entry", "entry": generic.entry, "response_signal": selection},
        {"mode": "simulink", "model": "../plant.slx"},
        {"mode": "simulink", "model": "plant.m"},
        {"mode": "entry", "entry": generic.entry, "working_directory": "../outside"},
        {"mode": "entry", "entry": generic.entry, "project_file": "project.m"},
        {"mode": "entry", "entry": generic.entry, "conditions": {"bad": float("inf")}},
        {"mode": "entry", "entry": generic.entry, "trusted": True},
    ]
    for data in bad:
        with pytest.raises(ValidationError):
            contracts.ProjectExecutionInput.model_validate(data)


def test_stepinfo_and_local_request_result_are_typed_finite_and_correlated(tmp_path):
    contracts = verification_module("matlab_contracts")
    series = {
        "path": "series.json",
        "sha256": "a" * 64,
        "size": 42,
        "node_id": "execute",
        "id": "series",
    }
    step = contracts.StepinfoInput(series=series, settling_band=0.02)
    assert step.steady_state.tail_fraction == 0.2
    assert step.conditions == {}
    for band in (0, 1, -0.1, float("nan")):
        with pytest.raises(ValidationError):
            contracts.StepinfoInput(series=series, settling_band=band)
    with pytest.raises(ValidationError):
        contracts.StepinfoInput(series=series)
    project = discover(prepared_project(tmp_path / "case"))
    request = contracts.LocalMatlabRequest(
        id="1" * 32, operation="stepinfo", inputs=step, input_evidence=[series]
    )
    assert request.project is None and request.timeout_seconds == 300
    assert request.input_evidence[0].sha256 == "a" * 64
    assert contracts.LocalMatlabRequest.model_validate_json(request.model_dump_json()) == request
    execute = contracts.LocalMatlabRequest(
        id="2" * 32,
        operation="execute_project",
        project=project,
        inputs={"mode": "entry", "entry": project.entries[0].model_dump(mode="json")},
    )
    assert execute.inputs.entry.path == "initialize.m"
    for data in (
        {**request.model_dump(), "operation": "execute_project"},
        {**execute.model_dump(), "project": None},
        {**execute.model_dump(), "timeout_seconds": 1801},
        {
            **execute.model_dump(),
            "inputs": {"mode": "entry", "entry": {"path": "missing.m", "kind": "script"}},
        },
    ):
        with pytest.raises(ValidationError):
            contracts.LocalMatlabRequest.model_validate(data)
    result = contracts.LocalMatlabResult(
        id=request.id,
        request_sha256="b" * 64,
        execution_status="completed",
        valid=True,
        details={"series": series},
        artifacts=[series],
    )
    assert json.loads(result.model_dump_json())["id"] == request.id
    for status in ("failed", "interrupted", "unknown"):
        with pytest.raises(ValidationError):
            contracts.LocalMatlabResult(
                id=request.id, request_sha256="b" * 64, execution_status=status, valid=True
            )
    with pytest.raises(ValidationError):
        contracts.LocalMatlabResult(
            id=request.id,
            request_sha256="b" * 64,
            execution_status="completed",
            valid=True,
            details={"bad": float("nan")},
        )
