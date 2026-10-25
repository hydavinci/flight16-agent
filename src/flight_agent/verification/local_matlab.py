import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import IO, Literal, TypeVar

from pydantic import BaseModel

from ..matlab import _stop_worker
from ..runtime.capability_metadata import Availability
from ..runtime.contracts import fingerprint
from ..runtime.observations import OutputArtifact
from ..session import _write_json
from .artifacts import MAX_ARTIFACT_BYTES, describe_artifact
from .intake import safe_directory
from .matlab import MAX_LOG_BYTES, MAX_RESULT_BYTES
from .matlab_contracts import (
    LocalMatlabCheckpoint,
    LocalMatlabExecution,
    LocalMatlabRequest,
    LocalMatlabResult,
    MatlabEntry,
    MatlabExecutionMetadata,
    MatlabSeriesPair,
    ProjectExecutionInput,
)
from .matlab_projects import discover_matlab_project, snapshot_matlab_project
from .projects import file_digest

MAX_REQUEST_BYTES = 8_000_000
PROBE_SECONDS = 10
T = TypeVar("T", bound=BaseModel)


class _OutputLimitError(ValueError):
    pass


def _environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LLM_", "OPENAI_", "AZURE_OPENAI_", "LANGSMITH_", "MATLAB_WORKER_"))
    }


def _read_model(path: Path, model: type[T], limit: int = MAX_RESULT_BYTES) -> T:
    safe_directory(path.parent)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Missing or unsafe {path.name}")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"{path.name} exceeds the output limit")
    return model.model_validate_json(content)


def _signature(entry: MatlabEntry) -> Literal[0, 1]:
    if entry.kind == "script":
        return 0
    identifier = r"[A-Za-z][A-Za-z0-9_]{0,62}"
    match = re.fullmatch(
        rf"function\s+(?:({identifier})\s*=\s*)?({identifier})"
        rf"\s*(?:\(\s*({identifier}(?:\s*,\s*{identifier})*)?\s*\))?\s*;?",
        entry.signature or "",
    )
    if not match or match[2] != Path(entry.path).stem:
        raise ValueError(f"Unsupported existing function signature: {entry.path}")
    names = [item.strip() for item in (match[3] or "").split(",") if item.strip()]
    if (
        "varargin" in names
        or match[1] == "varargout"
        or len(names) != len(entry.arguments)
        or any(
            part.startswith(("+", "@")) or part == "private" for part in Path(entry.path).parts[:-1]
        )
    ):
        raise ValueError(f"Unsupported existing function signature/arguments: {entry.path}")
    return 1 if match[1] is not None else 0


def _execution_request(request: LocalMatlabRequest, directory: Path) -> LocalMatlabExecution:
    assert isinstance(request.inputs, ProjectExecutionInput) and request.project is not None
    snapshot = directory / "project"
    entry, initializer = request.inputs.entry, request.inputs.initializer
    return LocalMatlabExecution(
        id=request.id,
        request_sha256=fingerprint(request.model_dump(mode="json")),
        snapshot_root=str(snapshot),
        output_directory=str(directory),
        inputs=request.inputs,
        project_paths=sorted(
            {str(snapshot), *(str((snapshot / item.path).parent) for item in request.project.files)}
        ),
        entry_nargout=_signature(entry) if entry is not None else 0,
        initializer_nargout=_signature(initializer) if initializer is not None else 0,
        entry_arguments=[
            json.dumps(value, ensure_ascii=False, allow_nan=False) for value in entry.arguments
        ]
        if entry is not None
        else [],
        initializer_arguments=[
            json.dumps(value, ensure_ascii=False, allow_nan=False)
            for value in initializer.arguments
        ]
        if initializer is not None
        else [],
    )


def _output_bounds(directory: Path) -> None:
    total = 0
    count = 0
    for current, directories, files in os.walk(directory, followlinks=False):
        if Path(current) == directory:
            directories[:] = [name for name in directories if name != "project"]
        for name in directories:
            if (Path(current) / name).is_symlink():
                raise ValueError("Output directory symlink is not allowed")
        for name in files:
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Output symlink or special file: {name}")
            size = path.stat().st_size
            limit = (
                MAX_REQUEST_BYTES
                if name in {"request.json", "execution.json"}
                else MAX_LOG_BYTES
                if name == "matlab.log"
                else MAX_RESULT_BYTES
                if path.suffix == ".json"
                else MAX_ARTIFACT_BYTES
            )
            total += size
            count += 1
            if size > limit or total > MAX_ARTIFACT_BYTES or count > 200:
                raise _OutputLimitError(f"MATLAB output exceeds the size/count limit: {name}")


def _verify_result(request: LocalMatlabRequest, result: LocalMatlabResult, directory: Path) -> None:
    if result.id != request.id or result.request_sha256 != fingerprint(
        request.model_dump(mode="json")
    ):
        raise ValueError("Receipt identity does not match the canonical request")
    for item in result.artifacts:
        actual = describe_artifact(directory, item.path)
        if (actual.sha256, actual.size) != (item.sha256, item.size):
            raise ValueError(f"Artifact digest mismatch: {item.path}")
    if not result.valid:
        return
    paths = {item.path for item in result.artifacts}
    if not {"raw.mat", "runtime-metadata.json", "metadata.json"} <= paths or not result.details.get(
        "output_evidence"
    ):
        raise ValueError("Missing actual output evidence")
    metadata = _read_model(directory / "metadata.json", MatlabExecutionMetadata)
    if (
        metadata.id != request.id
        or metadata.request_sha256 != result.request_sha256
        or metadata.inputs != request.inputs
        or metadata.conditions != request.inputs.conditions
        or metadata.snapshot_root != str(directory / "project")
    ):
        raise ValueError("Execution metadata identity differs from the confirmed request")
    if (
        isinstance(request.inputs, ProjectExecutionInput)
        and request.inputs.input_signal is not None
    ):
        if "series.json" not in paths:
            raise ValueError("Missing requested series artifact")
        series = _read_model(directory / "series.json", MatlabSeriesPair)
        for samples, selection in (
            (series.input, request.inputs.input_signal),
            (series.response, request.inputs.response_signal),
        ):
            assert selection is not None
            if samples.time_unit != "s" or samples.unit != selection.unit:
                raise ValueError("Series unit does not match the normalized requested unit")
            if samples.name != ".".join(selection.path):
                raise ValueError("Series identity does not match the selected signal path")
        expected = next(
            item.model_dump(mode="json") for item in result.artifacts if item.path == "series.json"
        )
        if result.details.get("series") != expected:
            raise ValueError("Series reference does not match its artifact")


class LocalMatlabBackend:
    def __init__(self, interpreter: str):
        self.interpreter = interpreter

    @classmethod
    def from_environment(cls) -> "LocalMatlabBackend":
        return cls(os.environ.get("MATLAB_PYTHON_EXECUTABLE") or sys.executable)

    def availability(self) -> Availability:
        if os.environ.get("MATLAB_ALLOW_TRUSTED_PROJECTS") != "1":
            return Availability(state="unavailable", reason="Set MATLAB_ALLOW_TRUSTED_PROJECTS=1")
        # Disk-backed output, polling and group cleanup also bound broken import-time probes.
        try:
            with tempfile.TemporaryDirectory(prefix="flight-matlab-probe-") as temporary:
                path = Path(temporary) / "probe.log"
                with path.open("w+", encoding="utf-8") as log:
                    process = self._spawn("--probe", cwd=Path(temporary), log=log)
                    deadline = time.monotonic() + PROBE_SECONDS
                    try:
                        while process.poll() is None:
                            if time.monotonic() >= deadline or path.stat().st_size > 64_000:
                                raise ValueError(
                                    "Interpreter/package/Engine probe exceeded its limit"
                                )
                            time.sleep(0.05)
                    finally:
                        cleanup = _stop_worker(process, label="MATLAB readiness probe")
                    if cleanup.error:
                        raise ValueError(cleanup.error)
                if path.stat().st_size > 64_000:
                    raise ValueError("Interpreter/package/Engine probe exceeded its output limit")
                text = path.read_text("utf-8")
                if process.returncode != 0:
                    raise ValueError(text[-5000:] or "Interpreter/package/Engine import failed")
                if json.loads(text) != {"available": True}:
                    raise ValueError("Invalid interpreter/package/Engine probe result")
        except (OSError, ValueError) as exc:
            return Availability(
                state="unavailable", reason=f"Local interpreter unavailable: {exc}"[:6000]
            )
        return Availability(state="available")

    def _spawn(self, argument: str, *, cwd: Path, log: IO[str]) -> subprocess.Popen[str]:
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(
            [
                self.interpreter,
                "-u",
                "-m",
                "flight_agent.verification.local_matlab_worker",
                argument,
            ],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            text=True,
            env=_environment(),
            start_new_session=os.name == "posix",
            creationflags=creation_flags,
        )

    def execute(
        self, request: LocalMatlabRequest, directory: Path, *, resume: bool = False
    ) -> LocalMatlabResult:
        request = LocalMatlabRequest.model_validate(request)
        safe_directory(directory)
        directory = directory.resolve()
        if request.project is not None:
            source = Path(request.project.root).resolve()
            if directory.is_relative_to(source) or source.is_relative_to(directory):
                raise ValueError("Execution directory must not overlap the original project")
        digest = fingerprint(request.model_dump(mode="json"))
        if resume:
            return self._resume(request, directory)
        if directory.exists():
            raise ValueError(
                "Run directory already exists; use resume to reconcile the original ID"
            )
        directory.mkdir(parents=True)
        state = LocalMatlabCheckpoint(
            id=request.id, request_sha256=digest, status="running", interpreter=self.interpreter
        )
        _write_json(directory / "request.json", request.model_dump(mode="json"))
        _write_json(directory / "state.json", state.model_dump(mode="json"))
        status: Literal["failed", "unknown", "interrupted"] = "failed"
        try:
            if request.operation != "execute_project":
                raise ValueError("stepinfo execution is not implemented by the project bridge")
            assert isinstance(request.inputs, ProjectExecutionInput) and request.project is not None
            for entry in (request.inputs.entry, request.inputs.initializer):
                if entry is not None:
                    _signature(entry)
                    if entry.kind == "script" and any(
                        item.path == Path(entry.path).with_suffix(".p").as_posix()
                        for item in request.project.files
                    ):
                        raise ValueError(
                            f"Unsupported selected script with same-stem P-file: {entry.path}"
                        )
            available = self.availability()
            if available.state != "available":
                raise ValueError(available.reason)
            snapshot_matlab_project(request.project, directory / "project")
            execution = _execution_request(request, directory)
            _write_json(directory / "execution.json", execution.model_dump(mode="json"))
            _output_bounds(directory)
            process = None
            cleanup_error = None
            failure: OSError | ValueError | None = None
            with (directory / "matlab.log").open("w", encoding="utf-8") as log:
                try:
                    process = self._spawn(str(directory), cwd=directory, log=log)
                    state = state.model_copy(update={"pid": process.pid})
                    _write_json(directory / "state.json", state.model_dump(mode="json"))
                    deadline = time.monotonic() + request.timeout_seconds
                    while process.poll() is None:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("MATLAB execution timed out")
                        _output_bounds(directory)
                        time.sleep(0.05)
                    _output_bounds(directory)
                except (OSError, ValueError) as exc:
                    failure = exc
                finally:
                    if process is not None:
                        cleanup_error = _stop_worker(process, label="Local MATLAB worker").error
            status = "unknown"
            if cleanup_error:
                raise ValueError(f"Worker cleanup uncertain: {cleanup_error}")
            if failure:
                status = (
                    "interrupted"
                    if isinstance(failure, (TimeoutError, _OutputLimitError))
                    else "unknown"
                    if process is not None
                    else "failed"
                )
                raise ValueError(str(failure))
            if process is None or process.returncode != 0:
                raise ValueError("Worker exited without a verified terminal receipt")
            state = state.model_copy(update={"worker_exit_confirmed": True})
            _write_json(directory / "state.json", state.model_dump(mode="json"))
            result = self._receipt(request, directory)
            if discover_matlab_project(request.project.root) != request.project:
                raise ValueError("Source project changed during execution; review required")
            return self._finish(directory, state, result, receipt=True)
        except (OSError, ValueError) as exc:
            result = LocalMatlabResult(
                id=request.id,
                request_sha256=digest,
                execution_status=status,
                valid=False,
                diagnostics=[str(exc)[:6000]],
            )
            return self._finish(directory, state, result)
        except (KeyboardInterrupt, SystemExit):
            self._finish(
                directory,
                state,
                LocalMatlabResult(
                    id=request.id,
                    request_sha256=digest,
                    execution_status="unknown",
                    valid=False,
                    diagnostics=["Parent interrupted; worker termination remains unconfirmed."],
                ),
            )
            raise

    def _receipt(self, request: LocalMatlabRequest, directory: Path) -> LocalMatlabResult:
        try:
            result = _read_model(directory / "receipt.json", LocalMatlabResult)
            if result.details.get("engine_closed") is not True:
                raise ValueError("Engine cleanup has not been confirmed")
            _verify_result(request, result, directory)
            described = describe_artifact(directory, "matlab.log")
            log = OutputArtifact(path=described.path, sha256=described.sha256, size=described.size)
            result.artifacts.append(log)
            return LocalMatlabResult.model_validate(result)
        except (OSError, ValueError) as exc:
            raise ValueError(f"Invalid worker receipt/output: {exc}") from exc

    def _finish(
        self,
        directory: Path,
        state: LocalMatlabCheckpoint,
        result: LocalMatlabResult,
        *,
        receipt: bool = False,
    ) -> LocalMatlabResult:
        _write_json(directory / "result.json", result.model_dump(mode="json"))
        state = state.model_copy(
            update={
                "status": result.execution_status,
                "result_sha256": file_digest(directory / "result.json"),
                "receipt_sha256": file_digest(directory / "receipt.json") if receipt else None,
            }
        )
        _write_json(directory / "state.json", state.model_dump(mode="json"))
        return result

    def _resume(self, request: LocalMatlabRequest, directory: Path) -> LocalMatlabResult:
        digest = fingerprint(request.model_dump(mode="json"))
        try:
            original = _read_model(
                directory / "request.json", LocalMatlabRequest, MAX_REQUEST_BYTES
            )
            state = _read_model(directory / "state.json", LocalMatlabCheckpoint)
            if original != request or state.id != request.id or state.request_sha256 != digest:
                raise ValueError("Saved execution identity differs from the canonical request")
            if state.status == "running":
                # No PID inspection, signaling or dispatch on recovery. Only the original receipt.
                if (directory / "result.json").exists():
                    local_result = _read_model(directory / "result.json", LocalMatlabResult)
                    _verify_result(request, local_result, directory)
                    if not local_result.valid:
                        return self._finish(directory, state, local_result)
                if not state.worker_exit_confirmed:
                    raise ValueError(
                        "Unconfirmed worker termination; original execution is unknown"
                    )
                result = self._receipt(request, directory)
                return self._finish(directory, state, result, receipt=True)
            if state.receipt_sha256 is not None:
                _read_model(directory / "receipt.json", LocalMatlabResult)
                if file_digest(directory / "receipt.json") != state.receipt_sha256:
                    raise ValueError("Saved receipt digest mismatch")
            result = _read_model(directory / "result.json", LocalMatlabResult)
            if (
                file_digest(directory / "result.json") != state.result_sha256
                or result.execution_status != state.status
            ):
                raise ValueError("Saved terminal result digest/state mismatch")
            _verify_result(request, result, directory)
            return result
        except (OSError, ValueError) as exc:
            return LocalMatlabResult(
                id=request.id,
                request_sha256=digest,
                execution_status="unknown",
                valid=False,
                diagnostics=[f"Cannot reconcile original execution; no redispatch: {exc}"[:6000]],
            )


def _normalize_series(request: LocalMatlabRequest, directory: Path) -> None:
    assert isinstance(request.inputs, ProjectExecutionInput)
    series = _read_model(directory / "series.json", MatlabSeriesPair)
    factors = {
        "s": 1,
        "second": 1,
        "seconds": 1,
        "ms": 0.001,
        "millisecond": 0.001,
        "milliseconds": 0.001,
        "us": 0.000001,
        "microseconds": 0.000001,
        "ns": 0.000000001,
        "nanoseconds": 0.000000001,
        "min": 60,
        "minute": 60,
        "minutes": 60,
        "h": 3600,
        "hour": 3600,
        "hours": 3600,
        "days": 86400,
    }
    data = series.model_dump(mode="json")
    for role, item in (("input", series.input), ("response", series.response)):
        factor = factors.get(item.time_unit)
        if factor is None:
            raise ValueError(f"Unsupported actual time unit: {item.time_unit}")
        data[role]["time"] = [value * factor for value in item.time]
        data[role]["time_unit"] = "s"
    series = MatlabSeriesPair.model_validate(data)
    _write_json(directory / "series.json", series.model_dump(mode="json"))
