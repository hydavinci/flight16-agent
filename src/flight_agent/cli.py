import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .agent import AgentError, execute_agent
from .config import ModelSettings, create_model
from .knowledge import KnowledgeBase
from .matlab import MatlabRunner, engine_available
from .models import Parameters, RuleSet
from .session import AnalysisSession


def doctor(root: Path) -> int:
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    print(f"Project: {root}")
    available = engine_available()
    print(f"MATLAB Engine: {'package found' if available else 'not installed'}")
    print("MATLAB release/Python compatibility, runtime startup and license: not verified")
    for name in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"):
        print(f"{name}: {'configured' if os.environ.get(name, '').strip() else 'missing'}")
    ready = available
    try:
        ModelSettings.from_environment()
    except ValueError as exc:
        print(f"Model configuration: {exc}")
        ready = False
    try:
        rules = RuleSet.model_validate_json((root / "rules" / "demo.json").read_text("utf-8"))
        KnowledgeBase(root / "knowledge").validate_sources(rules)
        if not (root / "matlab" / "analyze_demo.m").is_file():
            raise ValueError("matlab/analyze_demo.m is missing")
        print("Demo assets: ready")
    except (OSError, ValueError) as exc:
        print(f"Demo assets: {exc}")
        ready = False
    print("No MATLAB process started and no model request sent.")
    return 0 if ready else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local directory-based verification agent")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("doctor", help="Inspect configuration without external calls")
    analyze = commands.add_parser("analyze", help="Analyze a JSON file using the real agent")
    web = commands.add_parser("web", help="Serve the local Web interface")
    worker = commands.add_parser("worker", help="Serve the trusted-project MATLAB worker")
    for command in (check, analyze, web, worker):
        command.add_argument("--project-root", type=Path, default=Path.cwd())
    web.add_argument("--port", type=int, default=8000)
    worker.add_argument("--port", type=int, default=9001)
    worker.add_argument("--host", default="127.0.0.1")
    analyze.add_argument("parameters", type=Path, help="JSON path, relative to the project root")
    analyze.add_argument(
        "--request", default="分析这份参数文件，检查所有适用要求，检索依据并解释结果。"
    )
    analyze.add_argument("--matlab-timeout", type=float, default=120)
    analyze.add_argument("--max-steps", type=int, default=20)
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    session: AnalysisSession | None = None
    try:
        load_dotenv(root / ".env", override=False)
        if args.command == "doctor":
            return doctor(root)
        if args.command in ("web", "worker"):
            import uvicorn

            if not 1 <= args.port <= 65535:
                raise ValueError("Port must be between 1 and 65535")
            if args.command == "web":
                from .web import create_web_app

                app = create_web_app(root)
                host = "127.0.0.1"
            else:
                from .worker import create_worker_app

                if os.name != "posix" and os.environ.get("MATLAB_ALLOW_TRUSTED_UPLOADS") == "1":
                    raise ValueError("The upload worker currently supports macOS and Linux")
                app = create_worker_app(root, os.environ.get("MATLAB_WORKER_TOKEN", ""))
                host = args.host
            uvicorn.run(app, host=host, port=args.port, workers=1)
            return 0
        parameter_path = (
            args.parameters if args.parameters.is_absolute() else root / args.parameters
        )
        if parameter_path.stat().st_size > 1_000_000:
            raise ValueError("Parameter JSON must not exceed 1 MB")
        parameters = Parameters.model_validate_json(parameter_path.read_text(encoding="utf-8"))
        runner = MatlabRunner(root, timeout_seconds=args.matlab_timeout)
        session = AnalysisSession(root, parameters, runner)
        model = create_model()
        session.metadata["model_name"] = getattr(model, "model_name", type(model).__name__)
        report = execute_agent(model, session, args.request, max_steps=args.max_steps)
        print(f"Report: {report}")
        print(f"Assessment: {session.metadata['assessment_status']} (demonstration only)")
        return 0
    except (AgentError, ValueError, OSError) as exc:
        if session is not None:
            session.fail(str(exc))
            print(f"Run artifacts: {session.run_dir}", file=sys.stderr)
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if session is not None:
            session.fail("Interrupted by user")
        print("Interrupted.", file=sys.stderr)
        return 130
