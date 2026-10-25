import importlib
import json
import os
import sys
from pathlib import Path

from ..runtime.contracts import fingerprint
from ..runtime.observations import OutputArtifact
from ..session import _write_json
from .artifacts import describe_artifact
from .local_matlab import (
    MAX_REQUEST_BYTES,
    _execution_request,
    _normalize_series,
    _output_bounds,
    _read_model,
    _verify_result,
)
from .matlab_contracts import (
    LocalMatlabExecution,
    LocalMatlabRequest,
    LocalMatlabResult,
    MatlabExecutionMetadata,
    MatlabProjectOutput,
    MatlabRuntimeMetadata,
    ProjectExecutionInput,
)
from .matlab_projects import discover_matlab_project


def _support() -> Path:
    return Path(__file__).resolve().parents[1] / "matlab_support"


def _child(directory: Path) -> None:
    request = _read_model(directory / "request.json", LocalMatlabRequest, MAX_REQUEST_BYTES)
    if os.environ.get("MATLAB_ALLOW_TRUSTED_PROJECTS") != "1":
        raise ValueError("Set MATLAB_ALLOW_TRUSTED_PROJECTS=1")
    if request.operation != "execute_project" or not isinstance(
        request.inputs, ProjectExecutionInput
    ):
        raise ValueError("Unsupported local MATLAB operation")
    digest = fingerprint(request.model_dump(mode="json"))
    execution = _read_model(directory / "execution.json", LocalMatlabExecution, MAX_REQUEST_BYTES)
    if execution != _execution_request(request, directory):
        raise ValueError("Execution locators/identity differ from the confirmed request")
    assert request.project is not None
    snapshot = discover_matlab_project(execution.snapshot_root)
    if snapshot.model_copy(update={"root": request.project.root}) != request.project:
        raise ValueError("Prepared snapshot changed before Engine execution")
    engine = None
    result = LocalMatlabResult(
        id=request.id, request_sha256=digest, execution_status="failed", valid=False
    )
    try:
        module = importlib.import_module("matlab.engine")
        engine = module.start_matlab()
        engine.cd(str(_support()), nargout=0)
        engine.addpath(str(_support()), nargout=0)
        helper = "flight_agent_run_project"
        resolved = engine.which(helper, nargout=1)
        if not isinstance(resolved, str) or Path(resolved).resolve() != _support() / (
            helper + ".m"
        ):
            raise ValueError("Packaged MATLAB helper resolution mismatch")
        engine.feval(helper, str(directory / "execution.json"), nargout=0)
        result = result.model_copy(update={"execution_status": "unknown"})
        _output_bounds(directory)
        output = _read_model(directory / "bridge.json", MatlabProjectOutput)
        if output.id != request.id or output.request_sha256 != digest:
            raise ValueError("Bridge output identity mismatch")
        allowed = {"raw.mat", "runtime-metadata.json", "series.json", "return.json"}
        if len(set(output.artifact_paths)) != len(output.artifact_paths):
            raise ValueError("Duplicate output artifact")
        if not set(output.artifact_paths) <= allowed:
            raise ValueError("Unexpected output artifact")
        if request.inputs.input_signal is None and "series.json" in output.artifact_paths:
            raise ValueError("Series output requires confirmed signal selections")
        if request.inputs.input_signal is not None:
            if "series.json" not in output.artifact_paths:
                raise ValueError("Missing requested series")
            _normalize_series(request, directory)
        if "runtime-metadata.json" not in output.artifact_paths:
            raise ValueError("Missing runtime metadata artifact")
        runtime = _read_model(directory / "runtime-metadata.json", MatlabRuntimeMetadata)
        if (
            runtime.id != request.id
            or runtime.request_sha256 != digest
            or runtime.snapshot_root != execution.snapshot_root
        ):
            raise ValueError("Runtime metadata identity differs from the confirmed request")
        # MATLAB supplies measured runtime fields only; canonical JSON never round-trips
        # through MATLAB null/array conversion. Unexpected provenance claims are rejected.
        metadata = MatlabExecutionMetadata.model_validate(
            {
                **runtime.model_dump(mode="json"),
                "inputs": request.inputs.model_dump(mode="json"),
                "conditions": request.inputs.conditions,
                "input_provenance": "canonical_request",
            }
        )
        _write_json(directory / "metadata.json", metadata.model_dump(mode="json"))
        artifacts = []
        for name in [*output.artifact_paths, "metadata.json"]:
            item = describe_artifact(directory, name)
            artifacts.append(OutputArtifact(path=item.path, sha256=item.sha256, size=item.size))
        details = {**output.details, "output_evidence": output.output_evidence}
        for artifact in artifacts:
            if artifact.path == "series.json":
                details["series"] = artifact.model_dump(mode="json")
        result = LocalMatlabResult(
            id=request.id,
            request_sha256=digest,
            execution_status="completed",
            valid=True,
            details=details,
            diagnostics=output.diagnostics,
            artifacts=artifacts,
        )
        _verify_result(request, result, directory)
    except Exception as exc:
        # Engine import/start/call failures are known; invalid/lost output is not a success receipt.
        result = LocalMatlabResult(
            id=request.id,
            request_sha256=digest,
            execution_status=(
                "unknown" if result.execution_status in ("unknown", "completed") else "failed"
            ),
            valid=False,
            diagnostics=[f"MATLAB output/execution: {exc}"[:6000]],
        )
    finally:
        try:
            if engine is not None:
                engine.quit()
            result.details["engine_closed"] = True
        except Exception as exc:
            result = LocalMatlabResult(
                id=request.id,
                request_sha256=digest,
                execution_status="unknown",
                valid=False,
                diagnostics=[f"Engine cleanup uncertain: {exc}"[:6000]],
                details={"engine_closed": False},
            )
    _write_json(directory / "receipt.json", result.model_dump(mode="json"))


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("Expected the original execution directory or --probe")
        if sys.argv[1] == "--probe":
            module = importlib.import_module("matlab.engine")
            if (
                not callable(module.start_matlab)
                or not (_support() / "flight_agent_run_project.m").is_file()
            ):
                raise ValueError("Engine or packaged MATLAB support is unavailable")
            print(json.dumps({"available": True}))
        else:
            _child(Path(sys.argv[1]))
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
