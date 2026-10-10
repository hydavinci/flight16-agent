import copy
import importlib
import math

import pytest
from pydantic import ValidationError


def core():
    try:
        return importlib.import_module("flight_agent.models")
    except ModuleNotFoundError:
        pytest.fail("Typed input/result/rule contracts have not been implemented")


def rules_module():
    try:
        return importlib.import_module("flight_agent.rules")
    except ModuleNotFoundError:
        pytest.fail("Deterministic assessment has not been implemented")


def test_parameters_preserve_units_and_values(parameter_data):
    params = core().Parameters.model_validate(parameter_data)
    assert params.mass_kg == 10.0
    assert params.wing_area_m2 == 0.8


@pytest.mark.parametrize("value", [0, -1, math.inf, math.nan, True, "10"])
def test_parameters_reject_invalid_mass(parameter_data, value):
    parameter_data["mass_kg"] = value
    with pytest.raises(ValidationError):
        core().Parameters.model_validate(parameter_data)


def test_parameters_reject_unknown_fields(parameter_data):
    parameter_data["matlab_command"] = "arbitrary code"
    with pytest.raises(ValidationError):
        core().Parameters.model_validate(parameter_data)


def test_results_reject_nonfinite_metrics(result_data):
    result_data["metrics"]["wing_loading"]["value"] = math.nan
    with pytest.raises(ValidationError):
        core().AnalysisResult.model_validate(result_data)


@pytest.mark.parametrize("value,expected", [(1.19, "pass"), (1.2, "pass"), (1.21, "fail")])
def test_rule_exact_boundary(result_data, rule_data, value, expected):
    models = core()
    result_data["metrics"]["required_lift_coefficient"]["value"] = value
    report = rules_module().assess(
        models.AnalysisResult.model_validate(result_data), models.RuleSet.model_validate(rule_data)
    )
    assert report.status == expected
    assert report.evaluated == report.applicable == report.total == 1
    assert report.checks[0].source_id == "demo.md"


@pytest.mark.parametrize(
    "operator,value,expected",
    [
        ("<", 1.2, "fail"),
        (">=", 1.2, "pass"),
        (">", 1.2, "fail"),
    ],
)
def test_other_comparison_boundaries(result_data, rule_data, operator, value, expected):
    models = core()
    result_data["metrics"]["required_lift_coefficient"]["value"] = value
    rule_data["rules"][0]["operator"] = operator
    assert (
        rules_module()
        .assess(
            models.AnalysisResult.model_validate(result_data),
            models.RuleSet.model_validate(rule_data),
        )
        .status
        == expected
    )


@pytest.mark.parametrize("problem", ["missing", "unit", "invalid", "version"])
def test_untrustworthy_results_are_undetermined(result_data, rule_data, problem):
    models = core()
    if problem == "missing":
        del result_data["metrics"]["required_lift_coefficient"]
    elif problem == "unit":
        result_data["metrics"]["required_lift_coefficient"]["unit"] = "N"
    elif problem == "invalid":
        result_data["valid"] = False
        result_data["diagnostics"] = ["Result outside demo assumptions"]
    else:
        result_data["model_version"] = "other-model"
    report = rules_module().assess(
        models.AnalysisResult.model_validate(result_data), models.RuleSet.model_validate(rule_data)
    )
    assert report.status == "undetermined"
    assert report.evaluated == 0
    assert report.applicable == 1
    assert report.checks[0].reason


@pytest.mark.parametrize("unit,status", [("1", "pass"), ("N", "undetermined")])
def test_assessment_preserves_measurement_unit(result_data, rule_data, unit, status):
    models = core()
    result_data["metrics"]["required_lift_coefficient"] = {"value": 0.5, "unit": unit}
    report = rules_module().assess(
        models.AnalysisResult.model_validate(result_data), models.RuleSet.model_validate(rule_data)
    )
    check = report.model_dump()["checks"][0]
    assert check["value"] == 0.5
    assert check.get("value_unit") == unit
    assert check["unit"] == "1"
    assert check["status"] == status


def test_missing_metric_has_no_measurement_unit(result_data, rule_data):
    models = core()
    del result_data["metrics"]["required_lift_coefficient"]
    report = rules_module().assess(
        models.AnalysisResult.model_validate(result_data), models.RuleSet.model_validate(rule_data)
    )
    check = report.model_dump()["checks"][0]
    assert check["value"] is None
    assert check.get("value_unit", "missing") is None
    assert check["unit"] == "1"
    assert check["status"] == "undetermined"


def test_inapplicable_requirements_are_not_passes(result_data, rule_data):
    models = core()
    rule_data["rules"][0]["condition"] = "another-condition"
    report = rules_module().assess(
        models.AnalysisResult.model_validate(result_data), models.RuleSet.model_validate(rule_data)
    )
    assert report.status == "not_applicable"
    assert report.applicable == report.evaluated == 0


def test_partial_coverage_cannot_pass(result_data, rule_data):
    models = core()
    missing = copy.deepcopy(rule_data["rules"][0])
    missing.update(id="MISSING", metric="unavailable")
    rule_data["rules"].append(missing)
    report = rules_module().assess(
        models.AnalysisResult.model_validate(result_data), models.RuleSet.model_validate(rule_data)
    )
    assert report.status == "undetermined"
    assert report.evaluated == 1
    assert report.applicable == 2


@pytest.mark.parametrize("problem", ["empty", "duplicate", "nonfinite", "source"])
def test_invalid_rule_sets_rejected(rule_data, problem):
    if problem == "empty":
        rule_data["rules"] = []
    elif problem == "duplicate":
        rule_data["rules"] *= 2
    elif problem == "nonfinite":
        rule_data["rules"][0]["threshold"] = math.inf
    else:
        rule_data["rules"][0]["source_id"] = "../private.md"
    with pytest.raises(ValidationError):
        core().RuleSet.model_validate(rule_data)


def knowledge_module():
    try:
        return importlib.import_module("flight_agent.knowledge")
    except ModuleNotFoundError:
        pytest.fail("Local knowledge retrieval has not been implemented")


def test_chinese_retrieval_returns_real_source(project):
    kb = knowledge_module().KnowledgeBase(project / "knowledge")
    hits = kb.search("升力系数要求")
    assert hits[0].source_id == "demo.md"
    assert "1.2" in hits[0].content
    assert len(hits[0].sha256) == 64


@pytest.mark.parametrize("query", ["", "   ", "unrelatedzzzz"])
def test_no_match_returns_no_invented_evidence(project, query):
    assert knowledge_module().KnowledgeBase(project / "knowledge").search(query) == []


def test_knowledge_rejects_escaping_symlink(project, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    (project / "knowledge" / "escape.md").symlink_to(outside)
    with pytest.raises(ValueError, match="outside"):
        knowledge_module().KnowledgeBase(project / "knowledge")


def test_missing_rule_source_is_explicit(project, rule_data):
    models = core()
    rule_data["rules"][0]["source_id"] = "missing.md"
    kb = knowledge_module().KnowledgeBase(project / "knowledge")
    with pytest.raises(ValueError, match="missing.md"):
        kb.validate_sources(models.RuleSet.model_validate(rule_data))
