"""Four bounded analyses of registered evidence, with no model or file mutation."""

import math
from bisect import bisect_left, bisect_right
from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ..models import Contract, FiniteNumber
from ..runtime.capabilities import Capability, CapabilityRegistry
from ..runtime.contracts import Digest, Name
from .diagnostic_data import (
    DiagnosticContext,
    DiagnosticMetric,
    DiagnosticOutput,
    FrequencyResponse,
    TimeSeries,
)


class _Window(Contract):
    start: FiniteNumber
    end: FiniteNumber

    @model_validator(mode="after")
    def ordered_window(self) -> Self:
        if self.start >= self.end:
            raise ValueError("Window requires start < end")
        return self


class WindowStatisticsInput(_Window):
    dataset: Name
    signal: Name


class CompareSignalsInput(_Window):
    observed_dataset: Name
    observed_signal: Name
    reference_dataset: Name
    reference_signal: Name
    alignment: Literal["linear_overlap"]
    reference_time_offset: FiniteNumber


class ParameterSensitivityInput(_Window):
    datasets: Annotated[list[Name], Field(min_length=2, max_length=16)]
    signal: Name
    parameter: Name

    @model_validator(mode="after")
    def unique_datasets(self) -> Self:
        if len(self.datasets) != len(set(self.datasets)):
            raise ValueError("Sensitivity dataset IDs must be unique")
        return self


class LocalLoopInput(Contract):
    dataset: Name
    loop: Name
    start_hz: FiniteNumber
    end_hz: FiniteNumber

    @model_validator(mode="after")
    def ordered_band(self) -> Self:
        if not 0 < self.start_hz < self.end_hz:
            raise ValueError("Frequency band requires 0 < start_hz < end_hz")
        return self


def _output(
    fingerprints: dict[str, Digest],
    metrics: dict[str, tuple[float, str]] | None = None,
    *,
    valid: bool = True,
    observations: tuple[str, ...] = (),
    unknowns: tuple[str, ...] = (),
) -> DiagnosticOutput:
    finite_metrics: dict[str, DiagnosticMetric] = {}
    problems = list(unknowns)
    for name, (value, unit) in (metrics or {}).items():
        if math.isfinite(value):
            finite_metrics[name] = DiagnosticMetric(value=value, unit=unit)
        else:
            valid = False
            problems.append(f"{name} cannot be represented with finite numerical precision")
    return DiagnosticOutput(
        valid=valid, metrics=finite_metrics, observations=list(observations),
        unknowns=problems, data_fingerprints=fingerprints,
    )


def _read(
    context: DiagnosticContext, dataset: str, fingerprints: dict[str, Digest]
) -> TimeSeries | FrequencyResponse:
    data = context.read_dataset(dataset)
    registration = next(item for item in context.project.manifest.datasets if item.id == dataset)
    fingerprints[dataset] = next(
        file.sha256 for file in context.project.files if file.path == registration.path
    )
    return data


def _window(
    data: TimeSeries | FrequencyResponse, signal: str, start: float, end: float,
) -> tuple[list[float], str] | str:
    if not isinstance(data, TimeSeries):
        return "This capability requires a time_series dataset"
    if signal not in data.signals:
        return f"Missing signal: {signal}"
    if start < data.time[0] or end > data.time[-1]:
        return "Insufficient window evidence: requested window is outside registered time data"
    values = data.signals[signal].values[bisect_left(data.time, start):bisect_right(data.time, end)]
    if len(values) < 2:
        return "Insufficient window evidence: at least two recorded samples are required"
    return values, data.signals[signal].unit


def _mean(values: list[float]) -> float:
    scale = max(abs(value) for value in values)
    return math.fsum(value / scale for value in values) / len(values) * scale if scale else 0.0


def _rms(values: list[float]) -> float:
    scale = max(abs(value) for value in values)
    if not scale:
        return 0.0
    return math.sqrt(math.fsum((value / scale) ** 2 for value in values) / len(values)) * scale


def _center(values: list[float]) -> tuple[float, list[float]]:
    scale = max(abs(value) for value in values) or 1.0
    scaled = [value / scale for value in values]
    mean = math.fsum(scaled) / len(scaled)
    return scale, [value - mean for value in scaled]


def _fraction(value: float, left: float, right: float) -> float:
    width = right - left
    if math.isfinite(width):
        return (value - left) / width
    scale = max(abs(left), abs(right))
    return (value / scale - left / scale) / (right / scale - left / scale)


def _blend(left: float, right: float, fraction: float) -> float:
    return left * (1.0 - fraction) + right * fraction


def _interpolate(times: list[float], values: list[float], time: float) -> float:
    index = bisect_left(times, time)
    if times[index] == time:
        return values[index]
    fraction = _fraction(time, times[index - 1], times[index])
    return _blend(values[index - 1], values[index], fraction)


def _window_statistics(
    context: DiagnosticContext, inputs: WindowStatisticsInput
) -> DiagnosticOutput:
    fingerprints: dict[str, Digest] = {}
    data = _read(context, inputs.dataset, fingerprints)
    window = _window(data, inputs.signal, inputs.start, inputs.end)
    if isinstance(window, str):
        return _output(fingerprints, valid=False, unknowns=(window,))
    values, unit = window
    return _output(fingerprints, {
        "count": (float(len(values)), "samples"),
        "mean": (_mean(values), unit),
        "minimum": (min(values), unit),
        "maximum": (max(values), unit),
        "rms": (_rms(values), unit),
        "range": (max(values) - min(values), unit),
    })


def _compare_signals(context: DiagnosticContext, inputs: CompareSignalsInput) -> DiagnosticOutput:
    fingerprints: dict[str, Digest] = {}
    observed = _read(context, inputs.observed_dataset, fingerprints)
    reference = _read(context, inputs.reference_dataset, fingerprints)
    if not isinstance(observed, TimeSeries) or not isinstance(reference, TimeSeries):
        return _output(
            fingerprints, valid=False, unknowns=("Comparison requires time_series data",)
        )
    if (
        inputs.observed_signal not in observed.signals
        or inputs.reference_signal not in reference.signals
    ):
        return _output(
            fingerprints, valid=False, unknowns=("Missing observed or reference signal",)
        )
    left = observed.signals[inputs.observed_signal]
    right = reference.signals[inputs.reference_signal]
    if left.unit != right.unit:
        return _output(fingerprints, valid=False, unknowns=("Signal units do not match",))
    shifted = [time + inputs.reference_time_offset for time in reference.time]
    if not all(math.isfinite(time) for time in shifted) or any(
        a >= b for a, b in pairwise(shifted)
    ):
        return _output(
            fingerprints, valid=False,
            unknowns=("Reference time offset exceeds finite coordinate resolution",),
        )
    begin = bisect_left(observed.time, max(inputs.start, shifted[0]))
    end = bisect_right(observed.time, min(inputs.end, shifted[-1]))
    if end - begin < 2:
        return _output(
            fingerprints, valid=False,
            unknowns=("Insufficient actual overlap: at least two paired samples are required",),
        )
    values = left.values[begin:end]
    aligned = [_interpolate(shifted, right.values, time) for time in observed.time[begin:end]]
    errors = [a - b for a, b in zip(values, aligned, strict=True)]
    if not all(math.isfinite(error) for error in errors):
        return _output(
            fingerprints, valid=False, unknowns=("Signal errors exceed finite numerical range",)
        )
    metrics = {
        "count": (float(len(errors)), "samples"),
        "rmse": (_rms(errors), left.unit),
        "mean_error": (_mean(errors), left.unit),
        "max_abs_error": (max(abs(error) for error in errors), left.unit),
    }
    _, x = _center(values)
    _, y = _center(aligned)
    xx, yy = math.fsum(value * value for value in x), math.fsum(value * value for value in y)
    unknowns: tuple[str, ...] = ()
    if xx and yy:
        xy = math.fsum(a * b for a, b in zip(x, y, strict=True))
        metrics["correlation"] = (max(-1.0, min(1.0, xy / math.sqrt(xx * yy))), "1")
    else:
        unknowns = ("Correlation is undefined for a constant signal or insufficient precision",)
    return _output(
        fingerprints, metrics, unknowns=unknowns,
        observations=("Observed minus reference, linear overlap only; no causal assertion.",),
    )


def _parameter_sensitivity(
    context: DiagnosticContext, inputs: ParameterSensitivityInput
) -> DiagnosticOutput:
    fingerprints: dict[str, Digest] = {}
    parameters: list[float] = []
    means: list[float] = []
    unit = ""
    observation = ("Observational association across registered datasets, not proof of causation.",)
    for dataset in inputs.datasets:
        data = _read(context, dataset, fingerprints)
        registration = next(
            item for item in context.project.manifest.datasets if item.id == dataset
        )
        if inputs.parameter not in registration.parameters:
            return _output(
                fingerprints, valid=False, observations=observation,
                unknowns=(f"Missing parameter metadata: {dataset}/{inputs.parameter}",),
            )
        window = _window(data, inputs.signal, inputs.start, inputs.end)
        if isinstance(window, str):
            return _output(fingerprints, valid=False, observations=observation, unknowns=(window,))
        values, signal_unit = window
        if unit and signal_unit != unit:
            return _output(
                fingerprints, valid=False, observations=observation,
                unknowns=("Signal units do not match across datasets",),
            )
        unit = signal_unit
        parameters.append(registration.parameters[inputs.parameter])
        means.append(_mean(values))
    x_scale, x = _center(parameters)
    y_scale, y = _center(means)
    xx, yy = math.fsum(value * value for value in x), math.fsum(value * value for value in y)
    if len(set(parameters)) < 2 or not xx:
        return _output(
            fingerprints, valid=False, observations=observation,
            unknowns=("At least two numerically distinguishable parameter values are required",),
        )
    xy = math.fsum(a * b for a, b in zip(x, y, strict=True))
    coefficient, exponent = math.frexp(xy / xx)
    y_mantissa, y_exponent = math.frexp(y_scale)
    x_mantissa, x_exponent = math.frexp(x_scale)
    try:
        gradient = math.ldexp(
            coefficient * y_mantissa / x_mantissa, exponent + y_exponent - x_exponent
        )
    except OverflowError:
        gradient = math.inf
    metrics = {
        "gradient": (gradient, f"{unit}/{inputs.parameter}"),
        "count": (float(len(parameters)), "datasets"),
    }
    unknowns: tuple[str, ...] = ()
    if yy:
        metrics["r_squared"] = (min(1.0, max(0.0, xy * xy / (xx * yy))), "1")
    else:
        unknowns = ("Fit quality r_squared is undefined for a constant response",)
    return _output(fingerprints, metrics, observations=observation, unknowns=unknowns)


_LOOP_OBSERVATIONS = (
    "Negative feedback; gain and recorded unwrapped phase interpolated in log-frequency.",
    "Smallest absolute signed margin selected independently; ties use the lowest frequency.",
    "Local recorded-frequency margins cannot prove complete closed-loop stability.",
)


def _local_loop(context: DiagnosticContext, inputs: LocalLoopInput) -> DiagnosticOutput:
    fingerprints: dict[str, Digest] = {}
    data = _read(context, inputs.dataset, fingerprints)
    if not isinstance(data, FrequencyResponse):
        return _output(
            fingerprints, valid=False, observations=_LOOP_OBSERVATIONS,
            unknowns=("Local loop analysis requires frequency_response data",),
        )
    if data.loop != inputs.loop:
        raise ValueError("Requested loop identity does not match the registered loop")
    if inputs.start_hz < data.frequency_hz[0] or inputs.end_hz > data.frequency_hz[-1]:
        return _output(
            fingerprints, valid=False, observations=_LOOP_OBSERVATIONS,
            unknowns=("Requested frequency band is outside recorded data; no extrapolation",),
        )
    logs = [math.log10(frequency) for frequency in data.frequency_hz]
    if any(a >= b for a, b in pairwise(logs)):
        return _output(
            fingerprints, valid=False, observations=_LOOP_OBSERVATIONS,
            unknowns=("Recorded frequencies exceed log-frequency numerical resolution",),
        )
    begin, end = math.log10(inputs.start_hz), math.log10(inputs.end_hz)
    points = [begin, *(value for value in logs if begin < value < end), end]
    gains = [_interpolate(logs, data.gain_db, value) for value in points]
    phases = [_interpolate(logs, data.phase_deg, value) for value in points]
    phase_margins: list[tuple[float, float]] = []
    gain_margins: list[tuple[float, float]] = []
    gain_plateau = phase_plateau = excessive_crossings = False
    crossing_count = 0
    for i in range(len(points) - 1):
        x0, x1 = points[i:i + 2]
        g0, g1 = gains[i:i + 2]
        p0, p1 = phases[i:i + 2]
        if g0 == g1 == 0:
            gain_plateau = True
        elif min(g0, g1) <= 0 <= max(g0, g1):
            fraction = _fraction(0.0, g0, g1)
            phase = _blend(p0, p1, fraction)
            phase_margins.append((phase % 360.0 - 180.0, _blend(x0, x1, fraction)))
        if p0 == p1:
            phase_plateau |= (p0 - 180.0) % 360.0 == 0.0
            continue
        first = math.ceil((min(p0, p1) - 180.0) / 360.0)
        last = math.floor((max(p0, p1) - 180.0) / 360.0)
        crossing_count += last - first + 1
        if crossing_count > 20000:
            excessive_crossings = True
            continue
        for k in range(first, last + 1):
            fraction = _fraction(180.0 + 360.0 * k, p0, p1)
            gain_margins.append((-_blend(g0, g1, fraction), _blend(x0, x1, fraction)))
    unknowns: list[str] = []
    if gain_plateau:
        phase_margins = []
        unknowns.append("Zero-dB plateau: phase margin has no isolated crossover")
    elif not phase_margins:
        unknowns.append("No 0 dB crossing in the recorded band; phase margin is unknown")
    if phase_plateau or excessive_crossings:
        gain_margins = []
        unknowns.append(
            "Odd-180-degree phase plateau: gain margin has no isolated crossover"
            if phase_plateau else "Phase crossing count exceeds the bounded limit of 20000"
        )
    elif not gain_margins:
        unknowns.append(
            "No odd-180-degree phase crossing in the recorded band; gain margin is unknown"
        )
    metrics: dict[str, tuple[float, str]] = {}
    for candidates, margin_name, crossing_name, unit in (
        (phase_margins, "phase_margin_deg", "gain_crossover_hz", "deg"),
        (gain_margins, "gain_margin_db", "phase_crossover_hz", "dB"),
    ):
        if candidates:
            margin, log_frequency = min(candidates, key=lambda item: (abs(item[0]), item[1]))
            frequency = (
                inputs.start_hz if log_frequency <= begin else
                inputs.end_hz if log_frequency >= end else 10.0 ** log_frequency
            )
            metrics[margin_name] = (margin, unit)
            metrics[crossing_name] = (frequency, "Hz")
    return _output(
        fingerprints, metrics, valid=bool(phase_margins and gain_margins),
        observations=_LOOP_OBSERVATIONS, unknowns=tuple(unknowns),
    )


def diagnostic_capabilities() -> CapabilityRegistry[DiagnosticContext]:
    registry: CapabilityRegistry[DiagnosticContext] = CapabilityRegistry()
    registry.register(Capability(
        name="window_statistics", version="1", description="Inclusive registered-window statistics",
        input_model=WindowStatisticsInput, output_model=DiagnosticOutput,
        effect="read_only", operation=_window_statistics,
    ))
    registry.register(Capability(
        name="compare_signals", version="1",
        description="Explicit linear-overlap signal comparison",
        input_model=CompareSignalsInput, output_model=DiagnosticOutput,
        effect="read_only", operation=_compare_signals,
    ))
    registry.register(Capability(
        name="parameter_sensitivity", version="1",
        description="Observational window-mean regression",
        input_model=ParameterSensitivityInput, output_model=DiagnosticOutput,
        effect="read_only", operation=_parameter_sensitivity,
    ))
    registry.register(Capability(
        name="local_loop", version="1", description="Local negative-feedback frequency margins",
        input_model=LocalLoopInput, output_model=DiagnosticOutput,
        effect="read_only", operation=_local_loop,
    ))
    return registry
