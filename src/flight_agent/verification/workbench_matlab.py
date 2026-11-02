import hashlib
import importlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import warnings
from pathlib import Path
from typing import IO

from PIL import Image
from pydantic import ValidationError

from ..json_io import write_json
from ..matlab import MatlabError, _stop_worker
from ..workbench_contracts import (
    MAX_WORKBENCH_FIGURE_BYTES,
    MAX_WORKBENCH_FIGURES,
    MAX_WORKBENCH_LOG_BYTES,
    MAX_WORKBENCH_RESULT_BYTES,
    WorkbenchArtifact,
    WorkbenchExecutionReceipt,
    WorkbenchRunRequest,
    workbench_request_digest,
)
from .intake import safe_directory

# Covers the fixed digest, bounded MATLAB version, and five-name figure manifest.
MAX_WORKBENCH_RECEIPT_METADATA_BYTES = 4_096
_RECEIPT_BYTES = MAX_WORKBENCH_RESULT_BYTES + MAX_WORKBENCH_RECEIPT_METADATA_BYTES
_ENVIRONMENT_PREFIXES = (
    "LLM_",
    "OPENAI_",
    "AZURE_OPENAI_",
    "LANGSMITH_",
    "MATLAB_WORKER_",
)


def _environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_ENVIRONMENT_PREFIXES)
    }


def _support() -> Path:
    return Path(__file__).resolve().parents[1] / "matlab_support"


def _spawn(directory: Path, log: IO[str]) -> subprocess.Popen[str]:
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
    interpreter = os.environ.get("MATLAB_PYTHON_EXECUTABLE") or sys.executable
    return subprocess.Popen(
        [
            interpreter,
            "-u",
            "-m",
            "flight_agent.verification.workbench_matlab",
            str(directory),
        ],
        cwd=directory,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        text=True,
        env=_environment(),
        start_new_session=os.name == "posix",
        creationflags=creation_flags,
    )


def _bounded_size(path: Path, limit: int, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        return
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError(f"{label} exceeds its output limit")


def _read_bounded(path: Path, limit: int, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Missing or unsafe {label}")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"{label} exceeds its output limit")
    return content


def _sanitize_log(content: bytes) -> str:
    text = content.decode("utf-8", errors="replace")
    return re.sub(r"[^\x09\x0a\x0d\x20-\x7e\u0080-\uffff]", "\ufffd", text)


def validate_workbench_png(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("PNG file missing, unsafe or exceeds the 2 MB limit") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("PNG artifact symlink is not allowed")
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_WORKBENCH_FIGURE_BYTES:
        raise ValueError("PNG file missing, unsafe or exceeds the 2 MB limit")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(path) as image:
                if image.format != "PNG":
                    raise ValueError("Invalid PNG image")
                if image.width * image.height > 16_000_000:
                    raise ValueError("Invalid PNG or image dimensions exceed limit")
                image.verify()
        except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ValueError(f"Invalid PNG image: {exc}") from exc


def _artifact(directory: Path, name: str) -> WorkbenchArtifact:
    path = directory / name
    if path.parent != directory:
        raise ValueError(f"Unexpected figure path: {name}")
    validate_workbench_png(path)
    content = _read_bounded(path, MAX_WORKBENCH_FIGURE_BYTES, "PNG")
    return WorkbenchArtifact(
        path=name,
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        media_type="image/png",
        role="figure",
    )


def _receipt(directory: Path, request: WorkbenchRunRequest) -> WorkbenchExecutionReceipt:
    content = _read_bounded(directory / "receipt.json", _RECEIPT_BYTES, "receipt")
    try:
        receipt = WorkbenchExecutionReceipt.model_validate_json(content)
    except ValidationError as exc:
        raise ValueError(f"Invalid receipt JSON/result: {exc}") from exc
    if receipt.request_sha256 != workbench_request_digest(request):
        raise ValueError("Receipt request digest mismatch")
    expected = [f"figure-{index}.png" for index in range(1, len(receipt.figures) + 1)]
    if receipt.figures != expected:
        raise ValueError("Unexpected figure manifest")
    return receipt


def execute_workbench(
    directory: Path, request: WorkbenchRunRequest
) -> tuple[WorkbenchExecutionReceipt, list[WorkbenchArtifact], str]:
    request = WorkbenchRunRequest.model_validate(request)
    safe_directory(directory)
    directory = directory.resolve()
    if directory.exists():
        raise ValueError("Run directory already exists")
    directory.mkdir(parents=True)
    code = directory / "code"
    code.mkdir()
    (code / request.filename).write_text(request.source, encoding="utf-8")
    write_json(directory / "request.json", request.model_dump(mode="json"))

    process: subprocess.Popen[str] | None = None
    failure: Exception | None = None
    cleanup_error: str | None = None
    log_path = directory / "matlab.log"
    with log_path.open("w", encoding="utf-8") as log_stream:
        try:
            process = _spawn(directory, log_stream)
            deadline = time.monotonic() + request.timeout_seconds
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError("MATLAB workbench execution timed out")
                _bounded_size(log_path, MAX_WORKBENCH_LOG_BYTES, "MATLAB log")
                _bounded_size(directory / "receipt.json", _RECEIPT_BYTES, "receipt")
                time.sleep(0.05)
            _bounded_size(log_path, MAX_WORKBENCH_LOG_BYTES, "MATLAB log")
            _bounded_size(directory / "receipt.json", _RECEIPT_BYTES, "receipt")
        except (OSError, ValueError, TimeoutError) as exc:
            failure = exc
        finally:
            if process is not None:
                cleanup_error = _stop_worker(
                    process, label="MATLAB workbench worker"
                ).error

    try:
        log = _sanitize_log(_read_bounded(log_path, MAX_WORKBENCH_LOG_BYTES, "MATLAB log"))
    except ValueError as exc:
        log = ""
        failure = failure or exc
    if cleanup_error:
        raise MatlabError(f"Worker cleanup uncertain: {cleanup_error}")
    if failure is not None:
        raise MatlabError(str(failure))
    if process is None or process.returncode != 0:
        message = log.strip()[-6000:] or "MATLAB worker exited without a verified receipt"
        raise MatlabError(message)
    try:
        receipt = _receipt(directory, request)
        artifacts = [_artifact(directory, name) for name in receipt.figures]
    except (OSError, ValueError) as exc:
        raise MatlabError(str(exc)) from exc
    return receipt, artifacts, log


def _child(directory: Path) -> None:
    request_content = _read_bounded(directory / "request.json", 1_200_000, "request")
    request = WorkbenchRunRequest.model_validate_json(request_content)
    source = _read_bounded(
        directory / "code" / request.filename,
        1_000_000,
        "MATLAB source",
    ).decode("utf-8")
    if source != request.source:
        raise ValueError("Prepared MATLAB source differs from the request")
    if os.environ.get("MATLAB_ALLOW_TRUSTED_PROJECTS") != "1":
        raise ValueError("Set MATLAB_ALLOW_TRUSTED_PROJECTS=1")

    engine = None
    error: BaseException | None = None
    try:
        module = importlib.import_module("matlab.engine")
        engine = module.start_matlab()
        support = _support()
        engine.addpath(str(support), nargout=0)
        helper = "flight_agent_execute_workbench"
        resolved = engine.which(helper, nargout=1)
        if not isinstance(resolved, str) or Path(resolved).resolve() != support / (
            helper + ".m"
        ):
            raise ValueError("Packaged MATLAB helper resolution mismatch")
        parameters = json.dumps(
            request.parameters,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        engine.feval(
            helper,
            str(directory),
            request.filename,
            request.kind,
            parameters,
            workbench_request_digest(request),
            MAX_WORKBENCH_FIGURES,
            nargout=0,
        )
    except BaseException as exc:
        error = exc
    finally:
        if engine is not None:
            try:
                engine.quit()
            except BaseException as exc:
                raise RuntimeError(f"MATLAB Engine cleanup uncertain: {exc}") from exc
    if error is not None:
        raise error


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("Expected the original workbench execution directory")
        _child(Path(sys.argv[1]))
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
