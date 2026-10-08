import operator

from .models import AnalysisResult, Assessment, Check, Rule, RuleSet, Status

COMPARISONS = {
    "<=": operator.le,
    ">=": operator.ge,
    "<": operator.lt,
    ">": operator.gt,
}


def _check(result: AnalysisResult, rules: RuleSet, rule: Rule) -> Check:
    metric = result.metrics.get(rule.metric)
    value = metric.value if metric is not None else None
    status: Status
    if rule.aircraft_type != result.aircraft_type or rule.condition != result.condition:
        status, reason = "not_applicable", "Aircraft type or condition does not match"
    elif result.model_version != rules.model_version:
        status, reason = "undetermined", "Analysis model version does not match the rule set"
    elif not result.valid:
        status, reason = "undetermined", "Analysis is invalid: " + "; ".join(result.diagnostics)
    elif metric is None:
        status, reason = "undetermined", "Required metric is missing"
    elif metric.unit != rule.unit:
        status, reason = "undetermined", f"Unit mismatch: {metric.unit!r} != {rule.unit!r}"
    else:
        status = "pass" if COMPARISONS[rule.operator](metric.value, rule.threshold) else "fail"
        reason = f"{metric.value:g} {rule.operator} {rule.threshold:g} [{rule.unit}] => {status}"
    return Check(
        rule_id=rule.id,
        metric=rule.metric,
        status=status,
        value=value,
        operator=rule.operator,
        threshold=rule.threshold,
        unit=rule.unit,
        source_id=rule.source_id,
        source_section=rule.source_section,
        reason=reason,
    )


def assess(result: AnalysisResult, rules: RuleSet) -> Assessment:
    checks = [_check(result, rules, rule) for rule in rules.rules]
    statuses = {check.status for check in checks}
    status: Status = next(
        item for item in ("fail", "undetermined", "pass", "not_applicable") if item in statuses
    )
    return Assessment(
        rule_version=rules.version,
        status=status,
        total=len(checks),
        applicable=sum(check.status != "not_applicable" for check in checks),
        evaluated=sum(check.status in ("pass", "fail") for check in checks),
        checks=checks,
    )
