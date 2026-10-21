import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
from pydantic import JsonValue, TypeAdapter

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


def directory_task(args: argparse.Namespace) -> int:
    address = urlsplit(args.server)
    if (
        address.scheme != "http" or address.hostname not in ("127.0.0.1", "localhost")
        or address.username is not None or address.password is not None
        or address.path not in ("", "/") or address.query or address.fragment
    ):
        raise ValueError("--server must be a local HTTP origin without credentials or a path")
    if not 1 <= args.timeout <= 3600:
        raise ValueError("--timeout must be between 1 and 3600 seconds")
    if args.action == "confirm":
        if not args.trusted_project:
            raise ValueError("Confirmation requires explicit --trusted-project consent")
        if args.version < 1:
            raise ValueError("--version must be a positive confirmed-baseline version")
    identifier = args.conversation
    if identifier is not None and not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ValueError("Expected a recorded conversation identifier")
    schema = TypeAdapter(dict[str, JsonValue])
    with httpx.Client(
        base_url=args.server.rstrip("/"), timeout=min(args.timeout, 30),
        follow_redirects=False, trust_env=False,
    ) as client:
        def request(
            method: str, path: str, body: dict[str, JsonValue] | None = None
        ) -> dict[str, JsonValue]:
            response = client.request(method, path, json=body)
            response.raise_for_status()
            return schema.validate_python(response.json())

        bootstrap = request("GET", "/api/bootstrap")
        token = bootstrap.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("The local server did not provide an API token")
        client.headers["X-Flight-Token"] = token
        base = "/api/conversations"
        if args.action == "propose":
            body: dict[str, JsonValue] = {"message": args.request}
            for field in ("project_root", "output_root", "scenario"):
                value = getattr(args, field)
                if value is not None:
                    body[field] = value
            if identifier is None and args.scenario is None:
                body["scenario"] = "auto"
            record = request(
                "POST", f"{base}/{identifier}/messages" if identifier else base, body
            )
        elif args.action == "status":
            record = request("GET", f"{base}/{identifier}")
        else:
            record = request(
                "POST", f"{base}/{identifier}/{args.action}",
                {"version": args.version, "trusted": True} if args.action == "confirm" else None,
            )
        actual_id = record.get("id")
        if (
            not isinstance(actual_id, str) or not re.fullmatch(r"[a-f0-9]{32}", actual_id)
            or identifier is not None and actual_id != identifier
        ):
            raise ValueError("Server returned a mismatched conversation identity")
        deadline = time.monotonic() + args.timeout
        while args.action != "status" and record.get("status") in ("planning", "running"):
            if time.monotonic() >= deadline:
                raise ValueError(
                    f"Task {actual_id} continues on the local server; use directory-task status"
                )
            record = request("GET", f"{base}/{actual_id}")
            if record.get("id") != actual_id:
                raise ValueError("Server returned a mismatched conversation identity")
            if record.get("status") in ("planning", "running"):
                time.sleep(0.2)
        print(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False))
        return 1 if record.get("status") in ("failed", "interrupted") else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local flight-engineering agent")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("doctor", help="Inspect configuration without external calls")
    analyze = commands.add_parser("analyze", help="Analyze a JSON file using the real agent")
    web = commands.add_parser("web", help="Serve the local Web interface")
    worker = commands.add_parser("worker", help="Serve the trusted-project MATLAB worker")
    directory = commands.add_parser(
        "directory-task",
        help="Use the local Web API for knowledge and persistent engineering tasks",
    )
    directory.add_argument("--server", default="http://127.0.0.1:8000")
    directory.add_argument("--timeout", type=int, default=360)
    actions = directory.add_subparsers(dest="action", required=True)
    propose = actions.add_parser("propose", help="Propose a task without executing it")
    propose.add_argument(
        "--scenario", choices=("auto", "d1", "d2"),
        help="New conversations default to auto; follow-ups preserve the selected mode",
    )
    propose.add_argument("--project-root", help="Worker project (D1) or Agent data directory (D2)")
    propose.add_argument("--output-root", help="Agent-visible delivery directory")
    propose.add_argument("--conversation", help="Continue an existing conversation")
    propose.add_argument("--request", required=True)
    confirm = actions.add_parser("confirm", help="Confirm a recorded baseline and execute")
    confirm.add_argument("--version", type=int, required=True)
    confirm.add_argument("--trusted-project", action="store_true")
    resume = actions.add_parser(
        "resume", help="Resume saved work without duplicating unknown calls"
    )
    status = actions.add_parser("status", help="Read durable state without modifying the task")
    for command in (confirm, resume, status):
        command.add_argument("conversation")
    for command in (check, analyze, web, worker):
        command.add_argument("--project-root", type=Path, default=Path.cwd())
    web.add_argument("--port", type=int, default=8000)
    web.add_argument(
        "--native-directory-picker",
        action="store_true",
        help="Use macOS folder dialogs when browser, Web and Worker run on this same desktop",
    )
    worker.add_argument("--port", type=int, default=9001)
    worker.add_argument("--host", default="127.0.0.1")
    analyze.add_argument("parameters", type=Path, help="JSON path, relative to the project root")
    analyze.add_argument(
        "--request", default="分析这份参数文件，检查所有适用要求，检索依据并解释结果。"
    )
    analyze.add_argument("--matlab-timeout", type=float, default=120)
    analyze.add_argument("--max-steps", type=int, default=20)
    args = parser.parse_args(argv)
    session: AnalysisSession | None = None
    try:
        if args.command == "directory-task":
            return directory_task(args)
        root = args.project_root.resolve()
        load_dotenv(root / ".env", override=False)
        if args.command == "doctor":
            return doctor(root)
        if args.command in ("web", "worker"):
            import uvicorn

            if not 1 <= args.port <= 65535:
                raise ValueError("Port must be between 1 and 65535")
            if args.command == "web":
                from .web import create_web_app

                app = create_web_app(root, native_directory_picker=args.native_directory_picker)
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
    except (AgentError, ValueError, OSError, httpx.HTTPError) as exc:
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
