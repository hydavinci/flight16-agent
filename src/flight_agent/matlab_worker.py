import importlib
import io
import sys
from pathlib import Path

from .matlab import MatlabError
from .models import AnalysisResult, Parameters


def call_engine(project_root: Path, parameters: Parameters) -> AnalysisResult:
    try:
        engine_module = importlib.import_module("matlab.engine")
    except (ImportError, OSError) as exc:
        raise MatlabError(
            "MATLAB Engine is unavailable. Install MATLAB and its matching Python Engine package "
            f"in this interpreter ({sys.executable}). Original error: {exc}"
        ) from exc

    output, errors = io.StringIO(), io.StringIO()
    engine = None
    try:
        engine = engine_module.start_matlab()
        matlab_dir = str(project_root / "matlab")
        engine.cd(matlab_dir, nargout=0, stdout=output, stderr=errors)
        engine.addpath(matlab_dir, nargout=0, stdout=output, stderr=errors)
        raw = engine.feval(
            "analyze_demo", parameters.model_dump_json(), nargout=1, stdout=output, stderr=errors
        )
        if not isinstance(raw, str):
            raise MatlabError("MATLAB entry must return a JSON string")
        try:
            return AnalysisResult.model_validate_json(raw)
        except ValueError as exc:
            raise MatlabError(f"Invalid MATLAB JSON result: {exc}") from exc
    except (engine_module.EngineError, engine_module.MatlabExecutionError) as exc:
        raise MatlabError(f"MATLAB Engine execution failed: {exc}") from exc
    finally:
        try:
            if engine is not None:
                engine.quit()
        finally:
            sys.stderr.write(output.getvalue() + errors.getvalue())


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("Worker requires a trusted project root")
        parameters = Parameters.model_validate_json(sys.stdin.read())
        result = call_engine(Path(sys.argv[1]), parameters)
        sys.stdout.write(result.model_dump_json())
        return 0
    except (MatlabError, ValueError, OSError, TypeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
