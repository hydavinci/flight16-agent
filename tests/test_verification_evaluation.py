import pytest

from tests.verification_helpers import (
    baseline_data,
    margin_result_data,
    step_result_data,
    verification_module,
)


def assess(baseline, result):
    contracts = verification_module("contracts")
    return verification_module("evaluation").evaluate(
        contracts.EvaluationBaseline.model_validate(baseline),
        contracts.AnalysisResult.model_validate(result),
    )


@pytest.mark.parametrize(
    "overshoot, settling, expected",
    [(10.0, 3.0, "pass"), (10.1, 2.0, "fail"), (1.0, 3.01, "fail")],
)
def test_step_criteria_boundaries(overshoot, settling, expected):
    result = assess(baseline_data(), step_result_data(overshoot, settling))
    assert result.status == expected
    assert len(result.checks) == 2
    assert all(
        check.evidence == ["step-response.mat", "step-response.png"] for check in result.checks
    )


def test_missing_required_metric_does_not_turn_partial_failure_into_final_failure():
    data = step_result_data(overshoot=20.0)
    del data["analyses"][0]["metrics"]["settling_time"]
    result = assess(baseline_data(), data)
    assert result.status == "undetermined"
    assert {check.status for check in result.checks} == {"fail", "undetermined"}


def test_wrong_metric_unit_is_not_compared_as_if_compatible():
    data = step_result_data()
    data["analyses"][0]["metrics"]["settling_time"]["unit"] = "ms"
    result = assess(baseline_data(), data)
    assert result.status == "undetermined"


def test_unreliable_steady_state_is_undetermined_even_with_numeric_values():
    data = step_result_data()
    data["analyses"][0]["valid"] = False
    data["analyses"][0]["diagnostics"] = ["No reliable steady state"]
    result = assess(baseline_data(), data)
    assert result.status == "undetermined"
    assert all("steady" in check.reason for check in result.checks)


def test_execution_failure_is_not_a_numeric_criteria_failure():
    data = step_result_data()
    data["analyses"][0].update(
        execution_status="failed", valid=False, metrics={}, diagnostics=["sim failed"]
    )
    assert assess(baseline_data(), data).status == "undetermined"


@pytest.mark.parametrize("gain, phase, expected", [(0.0, 0.0, "pass"), (-0.1, 5.0, "fail")])
def test_margin_criteria_are_compared_in_declared_units(gain, phase, expected):
    result = assess(baseline_data(("stability_margins",)), margin_result_data(gain, phase))
    assert result.status == expected


def test_valid_positive_infinite_gain_margin_is_recorded_without_nonstandard_json():
    data = margin_result_data(phase=60.0)
    data["analyses"][0]["metrics"]["gain_margin"] = {
        "value": None,
        "special": "positive_infinity",
        "unit": "dB",
        "reason": "No phase crossover for a valid loop",
    }
    result = assess(baseline_data(("stability_margins",)), data)
    assert result.status == "pass"
    assert result.checks[0].special == "positive_infinity"
    assert "Infinity" not in result.model_dump_json()


def test_undefined_margin_is_not_infinity_or_pass():
    data = margin_result_data()
    data["analyses"][0]["metrics"]["phase_margin"] = {
        "value": None,
        "special": "undefined",
        "valid": False,
        "unit": "deg",
        "reason": "Undefined phase margin",
    }
    assert assess(baseline_data(("stability_margins",)), data).status == "undetermined"


def test_combined_analysis_requires_both_branches():
    result = assess(
        baseline_data(("step_response", "stability_margins")),
        step_result_data(),
    )
    assert result.status == "undetermined"
    assert result.by_analysis == {"step_response": "pass", "stability_margins": "undetermined"}


def test_revised_confirmed_threshold_changes_judgement_not_measured_value():
    data = baseline_data()
    data["criteria"][0]["threshold"] = 4.0
    result = assess(data, step_result_data(overshoot=5.0))
    assert result.status == "fail"
    assert result.checks[0].value == 5.0
    assert result.checks[0].threshold == 4.0


def test_unconfirmed_baseline_cannot_authorize_compliance_judgement():
    with pytest.raises(ValueError, match="confirm"):
        assess(baseline_data(confirmed=False), step_result_data())


@pytest.mark.parametrize("field, value", [("project_fingerprint", "b" * 64), ("trim_point", 2)])
def test_analysis_context_must_match_baseline(field, value):
    data = step_result_data()
    data[field] = value
    with pytest.raises(ValueError, match="context|project|trim"):
        assess(baseline_data(), data)


@pytest.mark.parametrize(
    "change",
    [
        {"unit": "seconds"},
        {"operator": ">="},
        {"threshold": float("nan")},
    ],
)
def test_baseline_rejects_invalid_criteria(change):
    contracts = verification_module("contracts")
    data = baseline_data()
    data["criteria"][1].update(change)
    with pytest.raises(ValueError):
        contracts.EvaluationBaseline.model_validate(data)


def test_duplicate_criteria_are_rejected():
    contracts = verification_module("contracts")
    data = baseline_data()
    data["criteria"].append(data["criteria"][0])
    with pytest.raises(ValueError, match="unique|Duplicate"):
        contracts.EvaluationBaseline.model_validate(data)
