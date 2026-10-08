import importlib
import json
import os
import subprocess
import sys
import time
import warnings
from pathlib import Path

from PIL import Image

from .matlab import MatlabError, _stop_worker
from .upload_models import (
    MAX_FIGURE,
    MAX_FIGURES,
    MAX_LOG,
    MAX_RESULT,
    ExecutionOutput,
    UploadRequest,
)


def validate_png(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FIGURE:
        raise ValueError("PNG file missing, unsafe or exceeds the 2 MB limit")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(path) as image:
                if image.format != "PNG" or image.width * image.height > 16_000_000:
                    raise ValueError("Invalid PNG or image dimensions exceed limit")
                image.verify()
        except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ValueError(f"Invalid PNG image: {exc}") from exc


def execute_upload(job_dir: Path, request: UploadRequest) -> ExecutionOutput:
    job_dir = job_dir.resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    code_dir = job_dir / "code"
    code_dir.mkdir(exist_ok=True)
    (code_dir / request.filename).write_text(request.source, encoding="utf-8")
    (job_dir / "request.json").write_text(request.model_dump_json(), encoding="utf-8")
    log_path = job_dir / "matlab.log"
    result_path = job_dir / "output.json"
    with log_path.open("w", encoding="utf-8") as log:
        environment = os.environ.copy()
        for key in list(environment):
            if key.startswith(("LLM_", "OPENAI_", "AZURE_OPENAI_", "LANGSMITH_", "MATLAB_WORKER_")):
                del environment[key]
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "flight_agent.upload_engine", str(job_dir)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            text=True,
            env=environment,
        )
        deadline = time.monotonic() + request.timeout_seconds
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise MatlabError(
                        "MATLAB upload timed out; termination requested. "
                        "Check detached MATLAB processes before retry."
                    )
                if log_path.stat().st_size > MAX_LOG:
                    raise MatlabError("MATLAB log exceeds the output limit")
                if result_path.exists() and result_path.stat().st_size > MAX_RESULT:
                    raise MatlabError("MATLAB result exceeds the output limit")
                time.sleep(0.05)
        finally:
            if process.poll() != 0:
                _stop_worker(process)
    text = log_path.read_bytes()[:MAX_LOG].decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise MatlabError(f"MATLAB execution failed: {text[-4000:]}")
    try:
        if log_path.stat().st_size > MAX_LOG:
            raise ValueError("MATLAB log exceeds the output limit")
        if result_path.is_symlink() or not result_path.is_file():
            raise ValueError("MATLAB did not produce a result")
        if result_path.stat().st_size > MAX_RESULT:
            raise ValueError("MATLAB result exceeds the output limit")
        raw = json.loads(result_path.read_text("utf-8"))
        raw["log"] = text
        output = ExecutionOutput.model_validate(raw)
        for name in output.figures:
            validate_png(job_dir / name)
        return output
    except (ValueError, OSError) as exc:
        raise MatlabError(f"Invalid MATLAB result: {exc}") from exc


def _child(job_dir: Path) -> None:
    request = UploadRequest.model_validate_json((job_dir / "request.json").read_text("utf-8"))
    try:
        engine_module = importlib.import_module("matlab.engine")
    except (ImportError, OSError) as exc:
        raise MatlabError("MATLAB Engine is unavailable on the worker: " + str(exc)) from exc
    engine = None
    try:
        engine = engine_module.start_matlab()
        wrapper_dir = Path(__file__).parent / "resources"
        engine.cd(str(wrapper_dir), nargout=0)
        engine.addpath(str(wrapper_dir), nargout=0)
        engine.feval(
            "flight_agent_execute_upload",
            str(job_dir),
            request.filename,
            request.kind,
            json.dumps(request.parameters, ensure_ascii=False, allow_nan=False),
            float(MAX_FIGURES),
            nargout=0,
        )
    except (engine_module.EngineError, engine_module.MatlabExecutionError) as exc:
        raise MatlabError(str(exc)) from exc
    finally:
        if engine is not None:
            engine.quit()


if __name__ == "__main__":
    try:
        _child(Path(sys.argv[1]))
    except (MatlabError, OSError, ValueError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from error
