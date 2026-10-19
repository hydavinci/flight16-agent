import importlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from ..matlab import MatlabError, _stop_worker
from ..session import _write_json
from .artifacts import describe_artifact
from .contracts import AnalysisResult, AnalysisRunRequest
from .projects import ProjectInspector, snapshot_project

MAX_LOG_BYTES = 4_000_000
MAX_RESULT_BYTES = 4_000_000


def execute_project(
    directory: Path,
    request: AnalysisRunRequest,
    on_progress: Callable[[str], None] | None = None,
) -> AnalysisResult:
    project = ProjectInspector().inspect(request.project_root)
    if project.fingerprint != request.project_fingerprint:
        raise ValueError("Project fingerprint changed; a new baseline confirmation is required")
    if request.trim_point not in project.config.supported_trim_points:
        raise ValueError("The project does not support the requested trim point")
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "analysis.json").exists() or (directory / "project").exists():
        raise ValueError("This run directory already contains an execution; do not run it again")
    if on_progress:
        on_progress("preparing")
    snapshot_project(project, directory / "project")
    _write_json(directory / "request.json", request.model_dump())
    _write_json(directory / "project.json", project.model_dump())
    log_path = directory / "matlab.log"
    output_path = directory / "analysis.json"
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LLM_", "OPENAI_", "AZURE_OPENAI_", "LANGSMITH_", "MATLAB_WORKER_"))
    }
    failure: MatlabError | OSError | None = None
    cleanup_error: str | None = None
    last_progress = ""
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "flight_agent.verification.matlab", str(directory)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            text=True,
            env=environment,
            start_new_session=os.name == "posix",
            creationflags=creation_flags,
        )
        deadline = time.monotonic() + request.timeout_seconds
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise MatlabError(
                        "MATLAB verification timed out; owned-process termination requested. "
                        "Check the run before explicitly retrying."
                    )
                if log_path.stat().st_size > MAX_LOG_BYTES:
                    raise MatlabError("MATLAB verification log exceeds 4 MB")
                if output_path.exists() and output_path.stat().st_size > MAX_RESULT_BYTES:
                    raise MatlabError("MATLAB verification result exceeds 4 MB")
                progress = directory / "progress.json"
                if on_progress and progress.is_file() and not progress.is_symlink():
                    if progress.stat().st_size > 10_000:
                        raise MatlabError("Invalid MATLAB progress record")
                    stage = str(json.loads(progress.read_text("utf-8"))["stage"])
                    if stage != last_progress:
                        on_progress(stage)
                        last_progress = stage
                time.sleep(0.05)
        except (MatlabError, OSError, ValueError, KeyError) as exc:
            failure = exc if isinstance(exc, (MatlabError, OSError)) else MatlabError(str(exc))
        finally:
            if process.poll() != 0:
                cleanup = _stop_worker(process)
                cleanup_error = cleanup.error
                if cleanup_error:
                    log.write("\n" + cleanup_error + "\n")
    if failure is not None:
        if cleanup_error:
            raise MatlabError(f"{failure}\n{cleanup_error}") from failure
        raise failure
    if process.returncode != 0:
        text = log_path.read_bytes()[-MAX_LOG_BYTES:].decode("utf-8", errors="replace")
        raise MatlabError(f"MATLAB verification failed: {text[-6000:]}")
    try:
        if log_path.stat().st_size > MAX_LOG_BYTES:
            raise ValueError("MATLAB log exceeds the output limit")
        if output_path.is_symlink() or not output_path.is_file():
            raise ValueError("MATLAB did not produce analysis.json")
        if output_path.stat().st_size > MAX_RESULT_BYTES:
            raise ValueError("MATLAB result exceeds the output limit")
        result = AnalysisResult.model_validate_json(output_path.read_text("utf-8"))
        if (
            result.run_id != request.id
            or result.trim_point != request.trim_point
            or result.project_fingerprint != request.project_fingerprint
        ):
            raise ValueError("MATLAB result context does not match the confirmed request")
        if {item.kind for item in result.analyses} != set(request.analyses):
            raise ValueError("MATLAB did not return exactly the requested analyses")
        names = sorted({name for item in result.analyses for name in item.artifact_paths})
        artifacts = [describe_artifact(directory, name) for name in names]
        result = AnalysisResult.model_validate(
            {**result.model_dump(), "artifacts": [item.model_dump() for item in artifacts]}
        )
        if ProjectInspector().inspect(project.root).fingerprint != project.fingerprint:
            raise ValueError("Source project changed during execution; results require review")
        _write_json(output_path, result.model_dump())
        return result
    except (ValueError, OSError) as exc:
        raise MatlabError(f"Invalid MATLAB verification output: {exc}") from exc


def _child(directory: Path) -> None:
    AnalysisRunRequest.model_validate_json((directory / "request.json").read_text("utf-8"))
    try:
        module = importlib.import_module("matlab.engine")
    except (ImportError, OSError) as exc:
        raise MatlabError(f"MATLAB Engine is unavailable on the worker: {exc}") from exc
    engine = None
    failure: MatlabError | None = None
    try:
        engine = module.start_matlab()
        support = Path(__file__).resolve().parents[1] / "matlab_support"
        engine.cd(str(support), nargout=0)
        engine.addpath(str(support), nargout=0)
        engine.feval(
            "flight_agent_verify",
            str(directory),
            nargout=0,
        )
    except (module.EngineError, module.MatlabExecutionError) as exc:
        failure = MatlabError(str(exc))
    finally:
        if engine is not None:
            try:
                engine.quit()
            except (module.EngineError, module.MatlabExecutionError) as exc:
                failure = MatlabError(
                    f"{failure or 'Execution finished'}; Engine cleanup failed: {exc}"
                )
    if failure is not None:
        raise failure


if __name__ == "__main__":
    try:
        _child(Path(sys.argv[1]))
    except (MatlabError, OSError, ValueError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from error
