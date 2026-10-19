import json
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from PIL import Image

from tests.verification_helpers import (
    baseline_data,
    margin_result_data,
    project_tree,
    step_result_data,
    verification_module,
)


def report_input(tmp_path, both=True, version=1, grouping="combined"):
    contracts = verification_module("contracts")
    artifacts = verification_module("artifacts")
    projects = verification_module("projects")
    root = project_tree(tmp_path / f"source-{version}")
    project = projects.ProjectInspector().inspect(str(root))
    directory = tmp_path / "delivery"
    directory.mkdir(exist_ok=True)
    data = step_result_data()
    kinds = ["step_response"]
    if both:
        data["analyses"].extend(margin_result_data()["analyses"])
        kinds.append("stability_margins")
    data["project_fingerprint"] = project.fingerprint
    for analysis in data["analyses"]:
        for name in analysis["artifact_paths"]:
            path = directory / name
            if name.endswith(".png"):
                Image.new("RGB", (24, 24), (20, 30, 40)).save(path)
            else:
                path.write_bytes(b"MATLAB 5.0 MAT-file test fixture" + b" " * 128)
    names = [name for item in data["analyses"] for name in item["artifact_paths"]]
    data["artifacts"] = [
        artifacts.describe_artifact(directory, name).model_dump() for name in names
    ]
    result = contracts.AnalysisResult.model_validate(data)
    baseline = contracts.EvaluationBaseline.model_validate(
        {**baseline_data(kinds), "project_fingerprint": project.fingerprint, "version": version}
    )
    evaluation = verification_module("evaluation").evaluate(baseline, result)
    request = {
        "task_id": "9" * 32,
        "title": "Pitch verification",
        "project": project,
        "baseline": baseline,
        "result": result,
        "evaluation": evaluation,
        "grouping": grouping,
        "explanation": "Recorded metrics meet the explicitly confirmed thresholds.",
        "explanation_evidence": ["step_response.overshoot", "stability_margins.phase_margin"],
        "execution_record": {"calls": [{"tool": "analyze_project", "status": "completed"}]},
    }
    return directory, request


def document_text(path):
    with ZipFile(path) as document:
        xml = ElementTree.fromstring(document.read("word/document.xml"))
        text = "\n".join(
            node.text or ""
            for node in xml.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")
        )
        return text, document.namelist()


def test_report_contains_confirmed_units_all_analyses_and_embedded_figures(tmp_path):
    reports = verification_module("reports")
    directory, data = report_input(tmp_path)
    artifacts = reports.deliver_report(reports.ReportRequest(**data), directory)
    word = [item for item in artifacts if item.role == "report"]
    assert len(word) == 1
    text, entries = document_text(directory / word[0].path)
    for required in (
        "Pitch verification",
        "PASS",
        "step_response",
        "stability_margins",
        "overshoot",
        "settling_time",
        "gain_margin",
        "phase_margin",
        "<= 10",
        "<= 3",
        ">= 0",
        "dB",
        "deg",
        "5%",
        "trim point 1",
        data["project"].fingerprint,
        "test fixture",
        "LLM explanation",
    ):
        assert required in text
    assert any(name.startswith("word/media/") for name in entries)
    for item in artifacts:
        verification_module("artifacts").verify_artifact(directory, item)
    index = json.loads((directory / "delivery-v1/artifact-index.json").read_text())
    assert index["reports"][word[0].path] == ["step_response", "stability_margins"]
    evaluation = json.loads((directory / "delivery-v1/evaluation.json").read_text())
    assert evaluation["baseline"]["confirmed"]
    assert evaluation["evaluation"]["status"] == "pass"


def test_report_regrouping_reuses_numerical_evidence(tmp_path):
    reports = verification_module("reports")
    directory, first = report_input(tmp_path)
    reports.deliver_report(reports.ReportRequest(**first), directory)
    digests = [(item.path, item.sha256) for item in first["result"].artifacts]
    baseline = first["baseline"].model_copy(update={"version": 2})
    evaluation = verification_module("evaluation").evaluate(baseline, first["result"])
    second = {**first, "baseline": baseline, "evaluation": evaluation, "grouping": "separate"}
    output = reports.deliver_report(reports.ReportRequest(**second), directory)
    words = [item for item in output if item.role == "report"]
    assert len(words) == 2
    assert (directory / "delivery-v1/verification.docx").exists()
    index = json.loads((directory / "delivery-v2/artifact-index.json").read_text())
    assert sorted(index["reports"].values()) == [["stability_margins"], ["step_response"]]
    for name, digest in digests:
        assert verification_module("artifacts").describe_artifact(directory, name).sha256 == digest


@pytest.mark.parametrize("problem", ["missing", "tampered", "unconfirmed", "wrong_evaluation"])
def test_report_rejects_unverifiable_evidence_and_decisions(tmp_path, problem):
    reports = verification_module("reports")
    directory, data = report_input(tmp_path)
    if problem == "missing":
        (directory / "step-response.png").unlink()
    elif problem == "tampered":
        (directory / "step-response.mat").write_bytes(b"MATLAB altered file")
    elif problem == "unconfirmed":
        data["baseline"] = data["baseline"].model_copy(update={"confirmed": False})
    else:
        data["evaluation"] = data["evaluation"].model_copy(update={"status": "fail"})
    with pytest.raises((ValueError, OSError)):
        reports.deliver_report(reports.ReportRequest(**data), directory)


def test_infinite_and_undetermined_values_are_explicit_in_report(tmp_path):
    reports = verification_module("reports")
    contracts = verification_module("contracts")
    directory, data = report_input(tmp_path)
    result_data = data["result"].model_dump()
    result_data["analyses"][0]["metrics"]["overshoot"] = {
        "unit": "%",
        "valid": False,
        "special": "undefined",
        "reason": "No stable endpoint",
    }
    result_data["analyses"][1]["metrics"]["gain_margin"] = {
        "unit": "dB",
        "special": "positive_infinity",
    }
    result = contracts.AnalysisResult.model_validate(result_data)
    evaluation = verification_module("evaluation").evaluate(data["baseline"], result)
    output = reports.deliver_report(
        reports.ReportRequest(**{**data, "result": result, "evaluation": evaluation}), directory
    )
    report = next(item for item in output if item.role == "report")
    text, _ = document_text(directory / report.path)
    assert "UNDETERMINED" in text
    assert "positive_infinity" in text
    assert "No stable endpoint" in text
