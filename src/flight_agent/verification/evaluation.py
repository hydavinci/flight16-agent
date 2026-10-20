import math

from .contracts import (
    METRICS,
    AnalysisResult,
    Evaluation,
    EvaluationBaseline,
    EvaluationStatus,
    MetricCheck,
)


def _aggregate(statuses: list[EvaluationStatus]) -> EvaluationStatus:
    if not statuses or "undetermined" in statuses:
        return "undetermined"
    return "fail" if "fail" in statuses else "pass"


def evaluate(baseline: EvaluationBaseline, result: AnalysisResult) -> Evaluation:
    if not baseline.confirmed:
        raise ValueError("The evaluation baseline must be confirmed by the user")
    if baseline.project_fingerprint != result.project_fingerprint:
        raise ValueError("Result project context does not match the confirmed baseline")
    if baseline.trim_point != result.trim_point:
        raise ValueError("Result trim context does not match the confirmed baseline")
    checks = []
    for criterion in baseline.criteria:
        kind = METRICS[criterion.metric][0]
        analysis = result.analysis(kind)
        metric = analysis.metrics.get(criterion.metric) if analysis else None
        status: EvaluationStatus = "undetermined"
        if analysis is None:
            reason = "The required analysis has no result"
        elif analysis.execution_status != "completed" or not analysis.valid:
            reason = "; ".join(analysis.diagnostics) or "Analysis is not valid"
        elif metric is None:
            reason = "The required metric is missing"
        elif not metric.valid:
            reason = metric.reason
        elif metric.unit != criterion.unit:
            reason = f"Metric unit {metric.unit!r} does not match {criterion.unit!r}"
        else:
            value = metric.value
            if metric.special == "positive_infinity":
                value = math.inf
            elif metric.special == "negative_infinity":
                value = -math.inf
            if value is None:
                reason = "Metric has no defined comparable value"
            else:
                passed = (
                    value <= criterion.threshold
                    if criterion.operator == "<="
                    else value >= criterion.threshold
                )
                status = "pass" if passed else "fail"
                reason = "Meets the confirmed criterion" if passed else "Violates the criterion"
                if metric.reason:
                    reason += "; " + metric.reason
        checks.append(
            MetricCheck(
                analysis=kind,
                metric=criterion.metric,
                value=metric.value if metric else None,
                special=metric.special if metric else None,
                value_unit=metric.unit if metric else None,
                operator=criterion.operator,
                threshold=criterion.threshold,
                unit=criterion.unit,
                status=status,
                reason=reason,
                evidence=analysis.artifact_paths if analysis else [],
            )
        )
    by_analysis = {
        kind: _aggregate([check.status for check in checks if check.analysis == kind])
        for kind in baseline.analyses
    }
    warnings = [
        "Results apply only to the recorded model, working point and confirmed criteria.",
        "Final engineering conclusions require designer review and independent reference results.",
    ]
    if "stability_margins" in baseline.analyses:
        warnings.append(
            "Meeting 0 dB / 0 degrees does not establish sufficient robustness "
            "or nonlinear full-envelope stability."
        )
    return Evaluation(
        status=_aggregate(list(by_analysis.values())),
        baseline_version=baseline.version,
        checks=checks,
        by_analysis=by_analysis,
        warnings=warnings,
    )
