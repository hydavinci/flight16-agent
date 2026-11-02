# Reproducibility, CI, and Modularization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver Stage 6 with locked Python dependencies, software and browser CI, explicitly opt-in licensed MATLAB acceptance, and focused backend/browser modules without changing the supported v2 API or workbench behavior.

**Architecture:** Keep `flight_agent.web.create_web_app()` as the public composition entry point, move runtime construction into an application factory, and extract route groups without changing their handlers' domain behavior. Extract the existing workbench into six native browser ES modules behind the existing `agent.js` URL, sharing an explicit state object and using callbacks instead of circular imports. Preserve the completed Stages 2–5 implementations rather than reconstructing their persistence, scheduling, snapshots, or planner internals.

**Tech Stack:** Python 3.11+, uv 0.12.6, FastAPI, Pydantic v2, pytest, Ruff, mypy, setuptools/build, Python Playwright/Chromium, GitHub Actions, native HTML/CSS/JavaScript ES modules.

**Spec:** `docs/superpowers/specs/2026-10-29-v2-runtime-optimization-design.md`, especially “Application and Frontend Modules,” “Dependency Reproducibility and CI,” “Delivery Stages / Stage 6,” and “Verification.”

## Global Constraints

- Keep only the generic v2 Agent execution model.
- Preserve the current confirmation, permission, source-attribution, hashing, evidence, recovery, and deterministic-evaluation guarantees.
- There is no runtime type switch between old and new tasks. Every active task is an `AgentTask`, every plan is an `AgentPlan`, and every executable operation is a registered capability.
- Local MATLAB execution: default one slot, configurable but never unbounded.
- Do not add Redis, Celery, or another external service.
- Do not redesign the workbench appearance; preserve the existing appearance and interaction.
- There is no Node runtime or frontend compilation step.
- Add and commit `uv.lock`.
- Document `uv sync --extra dev` as the development bootstrap command.
- Keep supported Python at 3.11 and newer as declared by the package.
- Add GitHub Actions for Python 3.11 and 3.12.
- Run unit tests excluding the `matlab` marker, Ruff, mypy, and package build.
- Run browser tests in a separate CI job when their required browser is installed.
- Keep real MATLAB acceptance opt-in for a licensed local or self-hosted environment.
- Cache uv downloads, not virtual environments containing machine-specific MATLAB Engine installations.
- Dependencies used only by deleted v1 code are removed after import verification.
- Each stage receives targeted tests, full software-only verification, and a separate commit. Later stages may adjust boundaries discovered by earlier dependency inventory, but may not restore v1 compatibility.

---

## Execution Boundary and Prerequisites

This document is a plan, not an instruction to implement while authoring it. Plan
authoring changes only this file, does not generate `uv.lock`, does not run
`uv sync`, does not commit, and does not dispatch workers.

Implementation takes place in `/Users/yhe/Work/flight-agent/.worktrees/stages-2-6`
**after Stages 2–5 have landed and their tests pass**. The checkout inspected for
this plan still has the Stage 1 assembly. Consequently, line numbers below are
navigation anchors, not permission to overwrite later-stage changes. Function
names and extraction boundaries are authoritative.

Consume these already-approved interfaces and behavior:

- `EngineeringRuntime` owns its injected store, planner, executor, resources, and
  Stage 3 scheduling/locking objects. Pass the injected runtime by identity.
- `runtime.queue_message(conversation_id, message, project_root=None, *,
  output_root=None, location=None) -> Conversation`.
- `runtime.continue_submission(conversation_id) -> Conversation`.
- `runtime.prepare_decision(conversation_id, action, *, version=None,
  trusted=False) -> Callable[[], Conversation] | None`.
- `runtime.capability_catalog() -> list[dict[str, object]]`.
- `runtime.store.get(id)`, `recent()`, and
  `task_directory(conversation_id, task_id, output_root)`.
- `runtime.resources.locations()`, `browse(location_id, path)`, and existing
  `ResourceLocation`/`ResourceRegistry` contracts.
- The Stage 2 SQLite WAL store, migrations, recovery, and transactional writes;
  Stage 3 per-conversation locks and bounded scheduling lanes; Stage 4 immutable
  snapshot binding and reuse; Stage 5 context limits and redacted diagnostics.
  None is reimplemented in this stage.
- `SchedulerSettings` from `config.py`; `PlanningScheduler`,
  `ExecutionTurnScheduler`, `ExecutionScheduler` and `SchedulerClosed` from
  `runtime/scheduling.py`.
- Stage 3 `ConversationManager(runtime, planning, execution)`, its `close()`,
  `verification_router(manager, authorize)`, and
  `create_web_app(..., scheduler_settings=None)`. Web no longer uses
  `BackgroundRunner`; that class remains supported for Worker transport only.
- Stage 4 `ProjectSnapshotStore` from `verification/project_snapshots.py`,
  `executor.snapshots`, and `LocalMatlabBackend.snapshots`.

Read the prerequisite plans alongside the design:
`2026-10-31-sqlite-conversation-store.md`,
`2026-10-31-scheduler-isolation.md`,
`2026-10-31-immutable-project-snapshots.md`, and
`2026-10-31-planner-context-copilot-diagnostics.md`, all in
`docs/superpowers/plans/`. Stage 6 does not resolve or modify their implementation
details; it preserves their public interfaces.

If a prerequisite is absent, stop implementation before changing production
files; complete that stage first. In particular, do not transplant the Stage 1
global lock or single-admission `BusyError` policy over Stage 3.

All commands below run from the worktree root. Start execution with:

```bash
cd /Users/yhe/Work/flight-agent/.worktrees/stages-2-6
git --no-pager status --short
python -m pytest -m "not matlab" -q
```

Expected: existing work is understood and preserved; the completed Stage 2–5
software suite passes. Browser tests may still be opt-in at this preflight.
Never delete unrelated changes to obtain a clean tree.

## Decision and Trade-offs

| Option | Benefit | Cost / reason |
|---|---|---|
| Keep the two composition files intact | Smallest immediate diff | Reject: does not implement the approved module boundaries or give browser imports/package contents explicit tests. |
| Mechanically extract focused modules, preserving existing algorithms | Selected: independently testable ownership, no framework or build migration | More imports, explicit callback wiring, and a larger static allowlist; require dependency-graph, browser, and distribution tests. |

Use the second option. Do not add a generic dependency-injection framework,
frontend framework, event bus, route version, database abstraction, or new
application service layer.

## File Ownership and Dependency Map

Task numbers indicate sequential ownership, not concurrent editing permission.

| File | Owner | Responsibility |
|---|---|---|
| `pyproject.toml` | Tasks 1, 5 | Preserve Python floor/runtime dependencies; lock build tools, pin uv, register browser marker. |
| `uv.lock` | Task 1 | Generated universal lock, including the existing `dev` extra and build tools; never hand-authored. |
| `README.md` | Tasks 1, 6 | Working locked bootstrap and concise validation commands. |
| `docs/usage.md` | Tasks 1, 6 | Bootstrap, dedicated Engine environment warning, real acceptance opt-in. |
| `docs/architecture.md` | Task 6 | Actual module ownership, test commands, CI boundary, package deployment. |
| `src/flight_agent/application.py` | Task 2 | Runtime/backend construction and explicit runtime/manager/scheduler injection. |
| `src/flight_agent/web.py` | Tasks 2, 3 | Public factory, security middleware/dependencies, lifespan, router composition only. |
| `src/flight_agent/verification/routes.py` | Task 3 | Existing request models, Stage 3 `ConversationManager`, conversation handlers only. |
| `src/flight_agent/web_routes/__init__.py` | Task 3 | Package marker only. |
| `src/flight_agent/web_routes/errors.py` | Task 3 | Existing shared HTTP exception translation, unchanged status codes/logging. |
| `src/flight_agent/web_routes/artifacts.py` | Task 3 | Artifact lookup, persisted-directory identity and evidence verification, downloads. |
| `src/flight_agent/web_routes/metadata.py` | Task 3 | Capability catalog and registered resource locations. |
| `src/flight_agent/web_routes/configuration.py` | Task 3 | Existing bootstrap/readiness and directory browsing/native-picker routes. |
| `src/flight_agent/web_routes/pages.py` | Tasks 3, 4 | `/`, `/workbench`, explicit static-asset allowlist and MIME types. |
| `src/flight_agent/static/agent.html` | Task 4 | Change only the script tag to native module loading. |
| `src/flight_agent/static/agent.js` | Task 4 | Stable-URL two-line bootstrap entry; no state or UI logic. |
| `src/flight_agent/static/api.js` | Task 4 | Fetch, token header, JSON/error decoding; no DOM or application-state writes. |
| `src/flight_agent/static/state.js` | Task 4 | One state object, status labels, active-conversation predicate; no DOM/network. |
| `src/flight_agent/static/render.js` | Task 4 | DOM primitives, conversation/progress/control rendering, text-only rendering. |
| `src/flight_agent/static/task-evidence.js` | Task 4 | Baseline, consent reset, evaluation, source/evidence views, artifact downloads. |
| `src/flight_agent/static/resize-preferences.js` | Task 4 | Existing pointer/keyboard resizing and preference storage. |
| `src/flight_agent/static/bootstrap.js` | Task 4 | Startup, polling, selection, submission/decisions, history/directory interaction, event binding. |
| `tests/test_reproducibility.py` | Task 1 | Lock metadata, required packages and documentation bootstrap contract. |
| `tests/test_application_factory.py` | Task 2 | Injected-object identity, default-builder seam, background ownership/shutdown. |
| `tests/test_web_modules.py` | Task 3 | Exact route manifest, handler ownership and security parity. |
| `tests/test_frontend_modules.py` | Task 4 | Module entry/allowlist, acyclic imports, exported interfaces, forbidden tooling. |
| `tests/test_frontend_modules_browser.py` | Tasks 4, 5 | Real browser import graph, state/API interfaces and stale-response guards. |
| `tests/test_distribution.py` | Task 4 | Wheel/sdist include every native module, route module and MATLAB support file. |
| `tests/test_ci_contract.py` | Task 5 | Parsed workflow semantics and browser-marker completeness. |
| `tests/test_agent_browser.py` | Task 5 | Add browser marker; retain existing tests unchanged. |
| `tests/test_agent_layout_browser.py` | Task 5 | Add browser marker; retain existing tests unchanged. |
| `tests/test_agent_column_resize_browser.py` | Task 5 | Add browser marker; retain existing tests unchanged. |
| `tests/test_matlab_project_browser.py` | Task 5 | Add browser marker; retain existing tests unchanged. |
| `tests/test_local_matlab_integration.py` | Task 5 | Extend the existing real linearization acceptance with real `allmargin`/hash checks. |
| `.github/workflows/ci.yml` | Task 5 | Hosted software matrix, separate browser job, manual gated self-hosted MATLAB job. |

`static/agent.css`, MATLAB production bridges, runtime contracts, the SQLite
schema, scheduling algorithms, snapshot logic, and planner policies are **not**
Stage 6 implementation targets.

Backend dependencies point from HTTP adapters to runtime interfaces.
`application.py` may import infrastructure to construct it; runtime/domain
modules must not import `web.py`, `application.py`, or `web_routes`.

Frontend dependency direction:

```text
agent.js -> bootstrap.js -> api.js
                       -> state.js
                       -> render.js
                       -> task-evidence.js
                       -> resize-preferences.js
```

Rendering and task/evidence factories receive callbacks from bootstrap. Neither
imports bootstrap or the other factory. Do not expose mutable state on `window`.

### Task 1: Lock Dependencies and Make Bootstrap Reproducible

**Files:**
- Modify: `pyproject.toml:1-44` — build/dev dependencies and `[tool.uv]`.
- Create: `uv.lock`.
- Modify: `README.md` — “安装项目” and “开发与验证”.
- Modify: `docs/usage.md` — “安装 Python 项目”.
- Test: `tests/test_reproducibility.py`.

**Interfaces:**
- Consumes: existing `[project.optional-dependencies].dev`, not a newly invented dependency group.
- Produces: `uv sync --extra dev` for developers, `uv sync --locked --extra dev` for CI.
- Produces: uv `0.12.6` as the exact resolver version for this stage.
- Produces: locked `build`, `setuptools`, `wheel`, and `pyyaml` available through `dev`; `python -m build --no-isolation` uses that environment.
- Preserves: `requires-python = ">=3.11"` and all runtime dependencies retained by Stages 1–5.

- [ ] **Step 1: Write the failing lock/bootstrap contract**

Create `tests/test_reproducibility.py`:

```python
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_lock_covers_supported_python_and_development_tools():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text("utf-8"))
    assert project["project"]["requires-python"] == ">=3.11"
    assert lock["requires-python"] == ">=3.11"
    assert project["tool"]["uv"]["required-version"] == "==0.12.6"
    packages = {item["name"] for item in lock["package"]}
    assert {
        "flight-matlab-agent", "pytest", "ruff", "mypy", "build",
        "setuptools", "wheel", "pyyaml", "playwright",
    } <= packages
    assert "matlabengine" not in packages
    assert not any(name.startswith("flight-agent-v1") for name in packages)
    for package in lock["package"]:
        for artifact in [package.get("sdist"), *package.get("wheels", [])]:
            if artifact is not None:
                assert re.fullmatch(r"sha256:[a-f0-9]{64}", artifact["hash"])


def test_documented_bootstrap_uses_existing_dev_extra():
    for name in ("README.md", "docs/usage.md"):
        text = (ROOT / name).read_text("utf-8")
        assert "uv sync --extra dev" in text
        assert "python3 -m pip install uv==0.12.6" in text
        assert "uv sync --locked --extra dev" in text
```

- [ ] **Step 2: Run the new tests and observe the missing lock**

```bash
python -m pytest tests/test_reproducibility.py -q
```

Expected: FAIL with missing `uv.lock` and absent uv bootstrap text. Do not create
a synthetic lock to make the test pass.

- [ ] **Step 3: Add locked build/test tools without changing runtime dependencies**

Retain the existing `dev` entries and append these requirements:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8,<10",
    "ruff>=0.12",
    "mypy>=1.15",
    "build>=1.2",
    "playwright>=1.50,<2",
    "setuptools>=77",
    "wheel>=0.45",
    "pyyaml>=6,<7",
]

[tool.uv]
required-version = "==0.12.6"
```

`pyyaml` is a development-only workflow-contract parser. `setuptools` and `wheel`
are development-only build requirements, allowing locked, non-isolated builds.
Keep `[build-system]` and package-data configuration intact.

- [ ] **Step 4: Document a real bootstrap and its Engine boundary**

Replace the installation shell block in both README and usage with:

```bash
python3 -m pip install uv==0.12.6
uv sync --extra dev
source .venv/bin/activate
test -e .env || cp .env.example .env
```

Add this text immediately below each block:

```text
开发依赖使用已提交的 uv.lock；CI 使用 uv sync --locked --extra dev，
锁文件过期时直接失败，不在 CI 中重新解析依赖。更新依赖时先运行 uv lock，
检查 uv.lock 差异并重新运行完整软件验证，再一起提交 pyproject.toml 与 uv.lock。
uv sync 默认精确同步，可能移除锁文件之外安装的 MATLAB Engine。
不要在已有 Engine 的环境中直接执行该命令；真实 MATLAB 验收使用单独的、
已许可且 Python 版本匹配的专用环境，按开发验证章节的 --inexact 流程运行。
安装 Python 依赖不代表已安装 MATLAB、Engine、工具箱或许可证。
```

Replace README’s development command block with:

```bash
uv sync --extra dev
uv run --no-sync python -m pytest -m "not matlab" -q
uv run --no-sync python -m ruff check .
uv run --no-sync python -m mypy src
uv run --no-sync python -m build --no-isolation
```

Task 5 will refine the software marker expression once `browser` is registered.

- [ ] **Step 5: Generate the actual universal lock**

```bash
uv --version
uv lock
uv sync --locked --extra dev
uv lock --check
uv run --no-sync python -m pytest tests/test_reproducibility.py -q
uv run --no-sync python -m build --no-isolation
```

Expected: uv reports `0.12.6`; resolution covers the declared `>=3.11` range,
not only the current interpreter; the contract tests and build pass. Inspect
`git --no-pager diff -- pyproject.toml uv.lock` for unexpected dependency removals,
private index credentials, local absolute paths, and an accidentally locked
Engine. No dependency is removed merely because its name looks obsolete.

- [ ] **Step 6: Commit the reproducibility deliverable**

```bash
git add pyproject.toml uv.lock README.md docs/usage.md tests/test_reproducibility.py
git commit -m "build: lock development dependencies with uv"
```

Expected: one focused commit; no `.venv`, uv cache, Engine installation, build
artifact, credential, or user configuration is tracked.

### Task 2: Extract Application Construction and Explicit Injection

**Files:**
- Create: `src/flight_agent/application.py`.
- Modify: `src/flight_agent/web.py:47-90` — factory signature/construction/lifespan; retain middleware and handlers for this task.
- Test: `tests/test_application_factory.py`.
- Regression: `tests/test_agent_web.py`, `tests/test_web_surface.py`.

**Interfaces:**
- Produces: `ApplicationServices(runtime: EngineeringRuntime, manager: ConversationManager,
  resources: ResourceRegistry, local_backend: LocalMatlabBackend,
  backend: AnalysisBackend | None, owns_store: bool, owns_manager: bool)`.
- Produces: `ApplicationServices.close() -> None`, manager drain before owned
  store close; injected manager/store remain caller-owned.
- Produces: `build_application(root: Path, *, engineering_runtime:
  EngineeringRuntime | None = None, scheduler_settings: SchedulerSettings | None = None,
  conversation_manager: ConversationManager | None = None)
  -> ApplicationServices`.
- Preserves/extends: `create_web_app(root: Path, *, engineering_runtime:
  EngineeringRuntime | None = None, native_directory_picker: bool = False,
  scheduler_settings: SchedulerSettings | None = None,
  conversation_manager: ConversationManager | None = None) -> FastAPI`.
- Injection rule: an injected manager requires `engineering_runtime` to be that
  manager's exact runtime. Supplied settings must match the runtime executor's
  settings. Reject either mismatch before constructing backends or stores.
- To inject planning/turn schedulers, construct the existing
  `ConversationManager(runtime, planning_scheduler, turn_scheduler)` and pass it
  explicitly. The executor's capability scheduler remains inside the runtime.
- Injected managers/stores are caller-owned. Default managers drain before owned
  stores close. Preserve `app.state.conversation_manager` for Stage 3 tests.

- [ ] **Step 1: Write failing identity, scheduling, and ownership tests**

Create `tests/test_application_factory.py`:

```python
import pytest
from fastapi.testclient import TestClient

from flight_agent.runtime.scheduling import (
    ExecutionTurnScheduler,
    PlanningScheduler,
    SchedulerClosed,
)
from flight_agent.verification.routes import ConversationManager
from flight_agent.web import create_web_app
from tests.agent_helpers import make_agent_web, wait_for_conversation


def test_injected_runtime_manager_and_schedulers_keep_identity(tmp_path):
    fixture_app, runtime, calls, _ = make_agent_web(
        tmp_path / "fixture", [{"action": "answer", "text": "No execution."}],
    )
    fixture_app.state.conversation_manager.close()
    settings = runtime.executor.scheduler.settings
    planning = PlanningScheduler(settings.planning_workers, settings.planning_queue)
    execution = ExecutionTurnScheduler(settings.execution_turns)
    manager = ConversationManager(runtime, planning, execution)
    app = create_web_app(
        tmp_path / "app", engineering_runtime=runtime, conversation_manager=manager,
    )
    try:
        assert app.state.services.runtime is runtime
        assert app.state.services.resources is runtime.resources
        assert app.state.services.local_backend.snapshots is runtime.executor.snapshots
        assert app.state.services.manager is manager
        assert app.state.conversation_manager is manager
        assert manager.planning is planning
        assert manager.execution is execution
        with TestClient(app, base_url="http://127.0.0.1") as client:
            headers = {"x-flight-token": client.get("/api/bootstrap").json()["token"]}
            response = client.post(
                "/api/conversations", headers=headers, json={"message": "Explain RMSE."},
            )
            assert response.status_code == 202
            done = wait_for_conversation(
                client, response.json()["id"], headers,
                lambda item: item["status"] == "completed",
            )
            assert done["messages"][-1]["content"] == "No execution."
        assert app.state.services.owns_manager is False
        with planning.reserve():
            pass
        assert calls == []
        assert not (tmp_path / "app" / "runs").exists()
    finally:
        manager.close()


def test_default_manager_closes_without_closing_injected_store(tmp_path):
    fixture_app, runtime, _, _ = make_agent_web(tmp_path / "fixture", [])
    fixture_app.state.conversation_manager.close()
    saved = runtime.store.create()
    app = create_web_app(tmp_path / "app", engineering_runtime=runtime)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/bootstrap").status_code == 200
    assert app.state.services.runtime is runtime
    assert app.state.services.owns_manager is True
    assert app.state.services.owns_store is False
    with pytest.raises(SchedulerClosed):
        with app.state.conversation_manager.planning.reserve():
            pass
    assert runtime.store.get(saved.id).id == saved.id


def test_manager_cannot_be_bound_to_a_different_runtime(tmp_path):
    first_app, first, _, _ = make_agent_web(tmp_path / "first", [])
    second_app, second, _, _ = make_agent_web(tmp_path / "second", [])
    try:
        with pytest.raises(ValueError, match="manager and runtime"):
            create_web_app(
                tmp_path / "app", engineering_runtime=second,
                conversation_manager=first_app.state.conversation_manager,
            )
        assert first is not second
    finally:
        first_app.state.conversation_manager.close()
        second_app.state.conversation_manager.close()


def test_default_application_owns_manager_and_store(tmp_path, monkeypatch):
    from flight_agent.application import build_application

    services = build_application(tmp_path)
    closed = []
    original = services.runtime.store.close

    def close():
        closed.append(True)
        original()

    monkeypatch.setattr(services.runtime.store, "close", close)
    try:
        assert services.owns_manager is True
        assert services.owns_store is True
        services.close()
        assert closed == [True]
        with pytest.raises(SchedulerClosed):
            with services.manager.planning.reserve():
                pass
    finally:
        services.manager.close()


def test_injected_scheduler_settings_must_match_runtime(tmp_path):
    from dataclasses import replace

    fixture_app, runtime, _, _ = make_agent_web(tmp_path / "fixture", [])
    try:
        settings = runtime.executor.scheduler.settings
        different = replace(
            settings, planning_workers=1 if settings.planning_workers != 1 else 2,
        )
        with pytest.raises(ValueError, match="scheduler settings"):
            create_web_app(
                tmp_path / "app", engineering_runtime=runtime, scheduler_settings=different,
            )
    finally:
        fixture_app.state.conversation_manager.close()
```

- [ ] **Step 2: Observe failure at the new injection seam**

```bash
uv run --no-sync python -m pytest tests/test_application_factory.py -q
```

Expected: FAIL because `create_web_app` does not accept `conversation_manager`
or expose `app.state.services`.

- [ ] **Step 3: Move current construction into the application factory**

Create `application.py` with the following concrete Stage 2–5 composition.
Compare its constructor calls with the completed stages before moving them;
retain any additional Stage 5 planner configuration already present at those
calls. This is composition only, not a replacement for any constructor's body.

```python
from dataclasses import dataclass
from pathlib import Path

from .config import SchedulerSettings
from .runtime.capabilities import CapabilityRegistry
from .runtime.scheduling import (
    ExecutionScheduler,
    ExecutionTurnScheduler,
    PlanningScheduler,
)
from .verification.agent_planner import AgentPlanner
from .verification.agent_runtime import AgentContext, AgentExecutor
from .verification.backend import AnalysisBackend, RemoteSimulinkBackend
from .verification.coordinator import EngineeringRuntime
from .verification.diagnostic_planner import DiagnosticReviewer
from .verification.local_matlab import LocalMatlabBackend
from .verification.plugins import register_builtin_plugins
from .verification.project_snapshots import ProjectSnapshotStore
from .verification.resources import ResourceRegistry
from .verification.routes import ConversationManager
from .verification.sqlite_store import SQLiteConversationStore


@dataclass(frozen=True)
class ApplicationServices:
    runtime: EngineeringRuntime
    manager: ConversationManager
    resources: ResourceRegistry
    local_backend: LocalMatlabBackend
    backend: AnalysisBackend | None
    owns_store: bool
    owns_manager: bool

    def close(self) -> None:
        if self.owns_manager:
            self.manager.close()
        if self.owns_store:
            self.runtime.store.close()


def build_application(
    root: Path,
    *,
    engineering_runtime: EngineeringRuntime | None = None,
    scheduler_settings: SchedulerSettings | None = None,
    conversation_manager: ConversationManager | None = None,
) -> ApplicationServices:
    if conversation_manager is not None and (
        engineering_runtime is None or conversation_manager.runtime is not engineering_runtime
    ):
        raise ValueError("Injected manager and runtime must be the same objects")
    owns_store = engineering_runtime is None
    settings = scheduler_settings if scheduler_settings is not None else (
        engineering_runtime.executor.scheduler.settings
        if engineering_runtime is not None
        else SchedulerSettings.from_environment()
    )
    if engineering_runtime is not None and (
        engineering_runtime.executor.scheduler.settings != settings
    ):
        raise ValueError("Injected runtime and Web scheduler settings must match")
    local_backend = LocalMatlabBackend.from_environment()
    resources = (
        engineering_runtime.resources if engineering_runtime is not None else ResourceRegistry()
    )
    try:
        backend: AnalysisBackend | None = RemoteSimulinkBackend.from_environment()
    except ValueError:
        backend = None
    if engineering_runtime is None:
        conversations = SQLiteConversationStore(root / "runs" / "verification")
        snapshots = ProjectSnapshotStore(conversations.root / "snapshots")
        local_backend.snapshots = snapshots
        tools: CapabilityRegistry[AgentContext] = CapabilityRegistry(agent_mode=True)
        register_builtin_plugins(
            tools, resources, backend=backend,
            diagnostic_reviewer=DiagnosticReviewer(), local_backend=local_backend,
        )
        engineering_runtime = EngineeringRuntime(
            conversations,
            AgentPlanner(),
            AgentExecutor(
                conversations, tools, resources,
                scheduler=ExecutionScheduler(settings), snapshots=snapshots,
            ),
            resources,
        )
    else:
        local_backend.snapshots = engineering_runtime.executor.snapshots
    manager = conversation_manager if conversation_manager is not None else ConversationManager(
        engineering_runtime,
        PlanningScheduler(settings.planning_workers, settings.planning_queue),
        ExecutionTurnScheduler(settings.execution_turns),
    )
    return ApplicationServices(
        runtime=engineering_runtime,
        manager=manager,
        resources=resources,
        local_backend=local_backend,
        backend=backend,
        owns_store=owns_store,
        owns_manager=conversation_manager is None,
    )
```

`ConversationManager.close()` already stops admission and drains both schedulers
in Stage 3; do not reimplement that algorithm here. Calling `from_environment()`
with no arguments preserves the Stage 4 backend-injection seam. A supplied
runtime shares its existing snapshot store; do not create an unused second store.

- [ ] **Step 4: Replace only construction in the public factory**

Import `SchedulerSettings`, `ConversationManager`, and `build_application`;
extend the public signature as specified. Replace the construction statements,
including the Stage 3 manager construction, with:

```python
    services = build_application(
        root,
        engineering_runtime=engineering_runtime,
        scheduler_settings=scheduler_settings,
        conversation_manager=conversation_manager,
    )
    runtime = services.runtime
    resources = services.resources
    local_backend = services.local_backend
    backend = services.backend
    manager = services.manager
```

Replace the Stage 3 lifespan with the equivalent ownership-aware composition:

```python
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            services.close()
```

Keep manager drain before store close. Before returning the app, add:

```python
    app.state.services = services
    app.state.conversation_manager = manager
    return app
```

No router, error mapping, readiness response, token generation, middleware, or
picker patch seam changes in this task.

- [ ] **Step 5: Verify the factory and existing HTTP behavior**

```bash
uv run --no-sync python -m ruff check --select I,F401 --fix src/flight_agent/application.py src/flight_agent/web.py
uv run --no-sync python -m pytest tests/test_application_factory.py tests/test_scheduler_web.py tests/test_agent_web.py tests/test_web_surface.py -q
uv run --no-sync python -m mypy src
```

Expected: PASS. Injected schedulers handle the submission; app exit does not
close a borrowed manager/store, and default managers stop admission after
shutdown. No second default store or capability scheduler is created for an
injected runtime. Stage 3 queued/capacity/shutdown tests remain effective.

- [ ] **Step 6: Commit the factory extraction**

```bash
git add src/flight_agent/application.py src/flight_agent/web.py tests/test_application_factory.py
git commit -m "refactor: isolate web application construction and injection"
```

### Task 3: Extract Focused Route Groups Without Changing HTTP Contracts

**Files:**
- Modify: `src/flight_agent/web.py:1-247` — retain composition, guards, security headers, lifespan.
- Modify: `src/flight_agent/verification/routes.py:161-279` — remove metadata/artifact handlers and nested error translator only.
- Create: `src/flight_agent/web_routes/__init__.py`.
- Create: `src/flight_agent/web_routes/errors.py`.
- Create: `src/flight_agent/web_routes/artifacts.py`.
- Create: `src/flight_agent/web_routes/metadata.py`.
- Create: `src/flight_agent/web_routes/configuration.py`.
- Create: `src/flight_agent/web_routes/pages.py`.
- Test: `tests/test_web_modules.py`.
- Regression: `tests/test_web_surface.py`, `tests/test_agent_web.py`, `tests/test_directories.py`, `tests/test_native_directories.py`, `tests/test_agent_delivery.py`.

**Interfaces:**
- Preserves: `verification_router(manager: ConversationManager,
  authorize: Callable[[Request], None]) -> APIRouter`;
  it now owns conversation handlers only.
- Produces: `artifact_router(runtime: EngineeringRuntime, authorize:
  Callable[[Request], None]) -> APIRouter`.
- Produces: `metadata_router(runtime: EngineeringRuntime, authorize:
  Callable[[Request], None]) -> APIRouter`.
- Produces: `configuration_router(services: ApplicationServices, token: str,
  native_directory_picker: bool, picker: Callable[..., str | None],
  authorize: Callable[[Request], None])
  -> APIRouter`. Pass the current `web.choose_native_directory` callable at
  composition time so existing monkeypatches still target the callable in use.
- Produces: `pages_router(static: Path) -> APIRouter`.
- Produces: `request_errors() -> Iterator[None]`, decorated with `@contextmanager`.
- Mount order: pages, configuration, metadata, artifacts, conversations.
  `/api/conversations/capabilities` must precede `/{conversation_id}`.

Exact preserved routes:

| Method | Path | Owner |
|---|---|---|
| GET | `/`, `/workbench`, `/static/{name}` | pages |
| GET | `/api/bootstrap` | configuration; intentionally token-free |
| GET | `/api/directories` | configuration; authorized |
| POST | `/api/directories/select` | configuration; authorized |
| GET | `/api/resource-locations` | metadata; authorized |
| GET | `/api/conversations/capabilities` | metadata; authorized |
| GET, POST | `/api/conversations` | conversations; authorized |
| GET | `/api/conversations/{conversation_id}` | conversations; authorized |
| GET | `/api/conversations/{conversation_id}/view` | conversations; authorized |
| POST | `/api/conversations/{conversation_id}/messages` | conversations; authorized |
| POST | `/api/conversations/{conversation_id}/confirm` | conversations; authorized |
| POST | `/api/conversations/{conversation_id}/resume` | conversations; authorized |
| POST | `/api/conversations/{conversation_id}/retry` | conversations; authorized |
| GET | `/api/conversations/{conversation_id}/tasks/{task_id}/artifacts/{name:path}` | artifacts; authorized |

Do not add `/health`, OpenAPI, a new capability URL, a directory upload route,
or a legacy compatibility endpoint.

- [ ] **Step 1: Write failing route-ownership and exact-manifest tests**

Create `tests/test_web_modules.py`:

```python
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.agent_helpers import make_agent_web

PREFIX = "flight_agent.web_routes."
EXPECTED = {
    ("GET", "/"): PREFIX + "pages",
    ("GET", "/workbench"): PREFIX + "pages",
    ("GET", "/static/{name}"): PREFIX + "pages",
    ("GET", "/api/bootstrap"): PREFIX + "configuration",
    ("GET", "/api/directories"): PREFIX + "configuration",
    ("POST", "/api/directories/select"): PREFIX + "configuration",
    ("GET", "/api/resource-locations"): PREFIX + "metadata",
    ("GET", "/api/conversations/capabilities"): PREFIX + "metadata",
    ("GET", "/api/conversations"): "flight_agent.verification.routes",
    ("POST", "/api/conversations"): "flight_agent.verification.routes",
    ("GET", "/api/conversations/{conversation_id}"): "flight_agent.verification.routes",
    ("GET", "/api/conversations/{conversation_id}/view"): "flight_agent.verification.routes",
    ("POST", "/api/conversations/{conversation_id}/messages"): "flight_agent.verification.routes",
    ("POST", "/api/conversations/{conversation_id}/confirm"): "flight_agent.verification.routes",
    ("POST", "/api/conversations/{conversation_id}/resume"): "flight_agent.verification.routes",
    ("POST", "/api/conversations/{conversation_id}/retry"): "flight_agent.verification.routes",
    (
        "GET",
        "/api/conversations/{conversation_id}/tasks/{task_id}/artifacts/{name:path}",
    ): PREFIX + "artifacts",
}


def test_exact_route_manifest_and_ownership(tmp_path):
    app, _, _, _ = make_agent_web(tmp_path, [])
    actual = {
        (method, route.path): route.endpoint.__module__
        for route in app.routes if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert actual == EXPECTED


def test_every_extracted_api_keeps_auth_headers_and_catalog_precedence(tmp_path):
    app, _, _, _ = make_agent_web(tmp_path, [])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        bootstrap = client.get("/api/bootstrap")
        assert bootstrap.status_code == 200
        headers = {"x-flight-token": bootstrap.json()["token"]}
        for path in (
            "/api/conversations", "/api/conversations/capabilities",
            "/api/resource-locations", "/api/directories?target=output",
            "/api/conversations/" + "a" * 32 + "/tasks/" + "b" * 32 + "/artifacts/x",
        ):
            denied = client.get(path)
            assert denied.status_code == 403
            assert denied.headers["x-content-type-options"] == "nosniff"
            assert denied.headers["cache-control"] == "no-store"
        catalog = client.get("/api/conversations/capabilities", headers=headers)
        assert catalog.status_code == 200
        assert isinstance(catalog.json(), list)
        for path in ("/", "/api/bootstrap", "/static/agent.js", "/static/missing.js"):
            result = client.get(path)
            assert "script-src 'self'" in result.headers["content-security-policy"]
            assert "frame-ancestors 'none'" in result.headers["content-security-policy"]
            assert result.headers["cache-control"] == "no-store"
        assert client.get("/static/agent.html").status_code == 404
        assert client.get("/static/%2e%2e%2fweb.py").status_code == 404
        assert client.get("/api/conversations", headers={
            **headers, "Origin": "https://attacker.example",
        }).status_code == 403
```

- [ ] **Step 2: Observe ownership failure, not a domain regression**

```bash
uv run --no-sync python -m pytest tests/test_web_modules.py -q
```

Expected: FAIL because handlers still belong to `flight_agent.web` and artifact/
metadata handlers still belong to the combined conversation router.

- [ ] **Step 3: Extract handlers with their current bodies and imports**

Create the empty package marker. Move the current nested `request_errors`
function into `web_routes/errors.py`, preserving all Stage 3 mappings. The
complete resulting module is:

```python
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException

from ..runtime.scheduling import CapacityExceeded, SchedulerClosed

logger = logging.getLogger(__name__)


@contextmanager
def request_errors() -> Iterator[None]:
    try:
        yield
    except CapacityExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": "scheduler_capacity", "message": str(exc)},
            headers={"Retry-After": "1"},
        ) from exc
    except SchedulerClosed as exc:
        raise HTTPException(
            status_code=503, detail={"code": "scheduler_closed", "message": str(exc)},
        ) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown conversation or task") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        logger.exception("Conversation storage is unavailable")
        raise HTTPException(
            status_code=503, detail="Conversation storage is unavailable",
        ) from exc
```

Both `verification/routes.py` and `web_routes/artifacts.py` import this function.
Keep `ConversationManager` and its complete Stage 3 implementation and scheduler
annotations in `verification/routes.py` unchanged. The shared error translator
must retain Stage 3's `CapacityExceeded -> 429 / Retry-After: 1`,
`SchedulerClosed -> 503`, and conversation-specific conflict mappings.
Remove its now-unused `FileResponse`, `verify_evidence`, `Path`, `Iterator`, and
`contextmanager` imports if no longer used.

The artifact router wrapper and handler are:

```python
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from ..runtime.evidence import verify_evidence
from ..verification.coordinator import EngineeringRuntime
from .errors import request_errors


def artifact_router(
    runtime: EngineeringRuntime,
    authorize: Callable[[Request], None],
) -> APIRouter:
    router = APIRouter(prefix="/api/conversations", dependencies=[Depends(authorize)])

    @router.get("/{conversation_id}/tasks/{task_id}/artifacts/{name:path}")
    def artifact(conversation_id: str, task_id: str, name: str) -> FileResponse:
        with request_errors():
            record = runtime.store.get(conversation_id)
            tasks = ([record.task] if record.task else []) + record.archived_tasks
            task = next((task for task in tasks if task.id == task_id), None)
            if task is None:
                raise KeyError("Unknown task")
            item = next((artifact for artifact in task.artifacts if artifact.path == name), None)
            if item is None:
                raise HTTPException(status_code=404, detail="Unknown artifact")
            directory = runtime.store.task_directory(conversation_id, task_id, task.output_root)
            if str(directory) != task.result_directory:
                raise ValueError("Task result directory does not match its persisted identity")
            path = verify_evidence(directory, item)
            return FileResponse(path, filename=Path(name).name)
        raise AssertionError("Unreachable")

    return router
```

The metadata module is complete:

```python
from collections.abc import Callable

from fastapi import APIRouter, Depends, Request

from ..verification.coordinator import EngineeringRuntime
from ..verification.resources import ResourceLocation


def metadata_router(
    runtime: EngineeringRuntime,
    authorize: Callable[[Request], None],
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(authorize)])

    @router.get("/api/conversations/capabilities")
    def capabilities() -> list[dict[str, object]]:
        return runtime.capability_catalog()

    @router.get("/api/resource-locations")
    def resource_locations() -> list[ResourceLocation]:
        return runtime.resources.locations()

    return router
```

The pages module initially preserves the existing three-asset allowlist:

```python
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

STATIC_ASSETS = {
    "agent.js": "text/javascript",
    "agent.css": "text/css",
    "favicon.svg": "image/svg+xml",
}


def pages_router(static: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/")
    def index() -> FileResponse:
        return FileResponse(static / "agent.html", media_type="text/html")

    @router.get("/workbench")
    def workbench() -> FileResponse:
        return FileResponse(static / "agent.html", media_type="text/html")

    @router.get("/static/{name}")
    def asset(name: str) -> FileResponse:
        if name not in STATIC_ASSETS:
            raise HTTPException(status_code=404, detail="Unknown asset")
        return FileResponse(static / name, media_type=STATIC_ASSETS[name])

    return router
```

Native module loading requires JavaScript MIME types with `nosniff`; explicit
MIME mapping avoids platform-specific `mimetypes` behavior.

- [ ] **Step 4: Mechanically extract bootstrap and directory handlers**

In `configuration.py`, move `DirectorySelectionRequest` unchanged and use the
following imports and wrapper:

```python
from collections.abc import Callable
from threading import Lock
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..application import ApplicationServices
from ..config import ModelSettings
from ..directories import (
    DirectoryListing,
    NativeDirectoryError,
    browse_directories,
)
from ..remote import RemoteError
from ..verification.agent_planner import AgentPlanner
from ..verification.backend import RemoteSimulinkBackend
from ..verification.resources import ResourceLocation, resolve_location
```

Start the wrapper with:

```python
def configuration_router(
    services: ApplicationServices,
    token: str,
    native_directory_picker: bool,
    picker: Callable[..., str | None],
    authorize: Callable[[Request], None],
) -> APIRouter:
    router = APIRouter()
    runtime = services.runtime
    resources = services.resources
    local_backend = services.local_backend
    backend = services.backend
    native_picker_lock = Lock()
    auth = [Depends(authorize)]
    choose_native_directory = picker
```

The native picker lock is per application, **not** per request. Preserve the `web.choose_native_directory`
patch seam by passing it to this wrapper.

Use this exact extraction recipe to append the current function bodies and
finish the wrapper; it avoids manually rewriting all directory validation:
run the Python block as standard input to `uv run --no-sync python -`.

```python
import ast
from pathlib import Path
from textwrap import dedent, indent

source = Path("src/flight_agent/web.py").read_text("utf-8")
tree = ast.parse(source)
factory = next(
    item for item in tree.body
    if isinstance(item, ast.FunctionDef) and item.name == "create_web_app"
)
functions = {
    item.name: item for item in factory.body if isinstance(item, ast.FunctionDef)
}
lines = source.splitlines(keepends=True)
destination = Path("src/flight_agent/web_routes/configuration.py")
text = destination.read_text("utf-8")
request_model = next(
    item for item in tree.body
    if isinstance(item, ast.ClassDef) and item.name == "DirectorySelectionRequest"
)
model_text = ast.get_source_segment(source, request_model) + "\n\n"
position = text.index("def configuration_router(")
text = text[:position] + model_text + text[position:]
for name in ("bootstrap", "project_location", "directories", "select_directory"):
    node = functions[name]
    start = min([node.lineno, *(item.lineno for item in node.decorator_list)]) - 1
    body = dedent("".join(lines[start:node.end_lineno]))
    body = body.replace("@app.", "@router.")
    text += "\n" + indent(body, "    ") + "\n"
text += "\n    return router\n"
ast.parse(text)
destination.write_text(text, "utf-8")
print("Extracted configuration handlers without changing their bodies")
```

After extraction, remove the original `DirectorySelectionRequest` and the moved
nested handlers from `web.py`. Remove only `resource_locations`, `capabilities`,
`artifact`, and `request_errors` from their old owners. Do not remove conversation
or directory validation models still in use.

- [ ] **Step 5: Compose routers and preserve all security/lifespan behavior**

The relevant composition block in `web.py` becomes:

```python
    app.state.services = services
    app.include_router(pages_router(Path(__file__).parent / "static"))
    app.include_router(configuration_router(
        services, token, native_directory_picker, choose_native_directory, authorize,
    ))
    app.include_router(metadata_router(runtime, authorize))
    app.include_router(artifact_router(runtime, authorize))
    app.include_router(verification_router(manager, authorize))
```

Keep the existing `same_origin`, `authorize`, `BodyLimitMiddleware`,
`TrustedHostMiddleware`, validation handler, response-header middleware, disabled
OpenAPI/docs URLs, random per-app token and current lifespan logic unchanged.
`app.state.services` is assigned once. Remove obsolete local aliases from Task 2
when no remaining composition statement uses them.

Run:

```bash
uv run --no-sync python -m ruff check --select I,F401 --fix src/flight_agent/web.py src/flight_agent/application.py src/flight_agent/web_routes src/flight_agent/verification/routes.py
uv run --no-sync python -m pytest tests/test_web_modules.py tests/test_web_surface.py tests/test_application_factory.py tests/test_agent_web.py tests/test_directories.py tests/test_native_directories.py tests/test_agent_delivery.py -q
uv run --no-sync python -m mypy src
```

Expected: PASS. Catalog requests are not parsed as conversation IDs. Native picker
monkeypatches still affect actual selection, remote-worker rejection and
single-dialog exclusion still hold, and artifact download/hash failure tests
retain their original results.

- [ ] **Step 6: Commit the focused route groups**

```bash
git add src/flight_agent/web.py src/flight_agent/verification/routes.py src/flight_agent/web_routes tests/test_web_modules.py
git commit -m "refactor: separate conversation artifact metadata and configuration routes"
```

### Task 4: Extract Native Browser Modules and Verify Shipped Assets

**Files:**
- Modify: `src/flight_agent/static/agent.html:9-11` — existing script tag only.
- Modify: `src/flight_agent/static/agent.js:1-935` — replace with stable module entry.
- Create: `src/flight_agent/static/api.js`.
- Create: `src/flight_agent/static/state.js`.
- Create: `src/flight_agent/static/render.js`.
- Create: `src/flight_agent/static/task-evidence.js`.
- Create: `src/flight_agent/static/resize-preferences.js`.
- Create: `src/flight_agent/static/bootstrap.js`.
- Modify: `src/flight_agent/web_routes/pages.py` — explicit six-asset additions.
- Test: `tests/test_frontend_modules.py`.
- Test: `tests/test_frontend_modules_browser.py`.
- Test: `tests/test_distribution.py`.
- Regression: all four existing `tests/*browser*.py` modules.

**Interfaces:**
- `state.js`: `createState() -> State`, `active(state) -> boolean`,
  `statuses` and `nodeStatuses`.
- `State` fields and initial values are shown in Step 3; no aliases/copies of
  mutable `current`, `selection`, `pendingView`, or timer state.
- `api.js`: `API = "/api/conversations"`,
  `createApi(getToken: () => string) -> {response, api}`.
- `response(path: string, options?: {method?: string, body?: object})
  -> Promise<Response>`; `api(...) -> Promise<object | object[]>`.
- `render.js`: `createRenderer(state, {closeDirectoryPicker, renderTask})
  -> {$, element, notice, resourceLocation, renderLocation, renderProgress,
  controls, directoryControls, table, details, messageText, render}`.
- `task-evidence.js`: `createTaskEvidence(state, {$, element, notice, table,
  details, response}) -> {sourceText, metricCard, renderChecks,
  renderEvaluation, renderTask, download, renderDelivery}`.
- `resize-preferences.js`: `initializeColumnResize(compact: MediaQueryList) -> void`.
- `bootstrap.js`: `start() -> Promise<void>` and no other exported mutable value.
- `agent.js`: imports `start`, invokes it once. Importing `bootstrap.js` alone
  must not attach handlers or start network requests.
- Factory callbacks are wired before `start()` executes; no circular module
  import or top-level DOM mutation is required.

Unchanged behavior includes:

- Chinese labels/copy; `textContent` rendering; no `innerHTML`.
- Structured request payloads and criterion/source text.
- 750 ms polling; generation guards on selection, view, history, directory and
  native-picker responses; no duplicate POST after an accepted request.
- Stage 3 `queued` labels, active-state detection, progress copy and disabled
  confirmation while queued; queued work continues polling.
- Consent reset on baseline/decision changes and new conversation.
- Current and archived-task downloads with token-bearing fetch, encoded path
  segments, original filenames, and object-URL revocation after 5000 ms.
- `verification-conversation` and `flight-agent-column-widths` storage keys.
- 260/340 px defaults; 200/240 px minima; 480 px center; 1100 px breakpoint;
  10/50 px keyboard increments; Enter/double-click reset; Escape/cancel restores
  preferences; compact layout, scroll/focus and draft preservation.

- [ ] **Step 1: Write failing native-module and distribution contracts**

Create `tests/test_frontend_modules.py`:

```python
import re
from pathlib import Path

from fastapi.testclient import TestClient

from tests.agent_helpers import make_agent_web

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/flight_agent/static"
MODULES = {
    "agent.js", "api.js", "state.js", "render.js", "task-evidence.js",
    "resize-preferences.js", "bootstrap.js",
}


def test_workbench_loads_native_modules_from_explicit_allowlist(tmp_path):
    app, _, _, _ = make_agent_web(tmp_path, [])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        html = client.get("/").text
        assert re.search(
            r'<script\b(?=[^>]*\btype="module")(?=[^>]*\bsrc="/static/agent\.js")[^>]*>',
            html,
        )
        for name in MODULES:
            response = client.get("/static/" + name)
            assert response.status_code == 200
            assert response.headers["content-type"].split(";")[0] == "text/javascript"
            assert response.headers["x-content-type-options"] == "nosniff"
        for name in ("agent.html", "unknown.js", "package.json", "uv.lock", "api.js.map"):
            assert client.get("/static/" + name).status_code == 404


def test_import_graph_is_local_complete_and_acyclic():
    imports = {}
    for name in MODULES:
        source = (STATIC / name).read_text("utf-8")
        dependencies = re.findall(r'\bfrom\s+["\']\./([^"\']+)["\']', source)
        assert set(dependencies) <= MODULES
        assert "innerHTML" not in source
        assert "require(" not in source
        assert "window.state" not in source
        imports[name] = dependencies

    def visit(name, visiting, visited):
        assert name not in visiting, f"Circular module dependency: {name}"
        if name in visited:
            return
        for dependency in imports[name]:
            visit(dependency, visiting | {name}, visited)
        visited.add(name)

    visited = set()
    visit("agent.js", set(), visited)
    assert visited == MODULES
    assert (STATIC / "agent.js").read_text("utf-8").strip() == (
        'import {start} from "./bootstrap.js";\n\nstart();'
    )
    for name in ("package.json", "package-lock.json", "webpack.config.js", "vite.config.js"):
        assert not (ROOT / name).exists()
    assert "fetch(" not in (STATIC / "state.js").read_text("utf-8")
    assert "document." not in (STATIC / "api.js").read_text("utf-8")
    assert "document." not in (STATIC / "state.js").read_text("utf-8")
```

Create `tests/test_distribution.py`:

```python
import subprocess
import sys
import tarfile
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def test_distributions_ship_native_modules_routes_and_matlab_bridges(tmp_path):
    subprocess.run(
        [
            sys.executable, "-m", "build", "--no-isolation",
            "--outdir", str(tmp_path),
        ],
        cwd=ROOT, check=True,
    )
    required = {
        path.relative_to(ROOT / "src").as_posix()
        for pattern in (
            "static/*.js", "static/*.html", "static/*.css", "static/*.svg",
            "matlab_support/*.m", "web_routes/*.py",
        )
        for path in (ROOT / "src/flight_agent").glob(pattern)
    }
    required.add("flight_agent/application.py")
    assert "flight_agent/static/bootstrap.js" in required
    wheel = next(tmp_path.glob("*.whl"))
    with ZipFile(wheel) as archive:
        assert required <= set(archive.namelist())
    sdist = next(tmp_path.glob("*.tar.gz"))
    with tarfile.open(sdist) as archive:
        names = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
        assert {"src/" + name for name in required} <= names
```

Create `tests/test_frontend_modules_browser.py`:

```python
import os
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.agent_helpers import make_agent_web
from tests.helpers import serve_app

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Browser tests are opt-in",
)


def test_native_imports_and_exported_state_transport_interfaces(tmp_path):
    app, _, calls, _ = make_agent_web(tmp_path, [])
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        errors, loaded = [], set()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("response", lambda result: loaded.add(urlsplit(result.url).path))
        try:
            page.goto(url)
            expect(page.locator("#send")).to_be_enabled()
            result = page.evaluate("""async () => {
                const {createState, active} = await import("/static/state.js");
                const {createApi} = await import("/static/api.js");
                const first = createState();
                const second = createState();
                first.current = {status: "queued"};
                const transport = createApi(() => "interface-test-token");
                const saved = window.fetch;
                let captured;
                window.fetch = async (path, options) => {
                    captured = {path, options};
                    return new Response(JSON.stringify({ok: true}), {
                        status: 200, headers: {"Content-Type": "application/json"},
                    });
                };
                try {
                    const payload = await transport.api("/api/example", {
                        method: "POST", body: {message: "literal <text>"},
                    });
                    return {firstActive: active(first), secondActive: active(second),
                        selection: second.selection, pendingView: second.pendingView,
                        captured, payload};
                } finally {
                    window.fetch = saved;
                }
            }""")
            assert result["firstActive"] is True
            assert result["secondActive"] is False
            assert result["selection"] == 0
            assert result["pendingView"] is None
            assert result["captured"]["options"]["headers"]["X-Flight-Token"] == (
                "interface-test-token"
            )
            assert result["captured"]["options"]["method"] == "POST"
            assert result["captured"]["options"]["body"] == '{"message":"literal <text>"}'
            assert result["payload"] == {"ok": True}
            assert {
                "/static/" + name for name in (
                    "agent.js", "api.js", "state.js", "render.js",
                    "task-evidence.js", "resize-preferences.js", "bootstrap.js",
                )
            } <= loaded
            assert calls == []
            assert errors == []
        finally:
            browser.close()


def test_late_selected_conversation_response_cannot_restore_old_selection(tmp_path):
    app, runtime, _, _ = make_agent_web(tmp_path, [])
    first = runtime.store.create()
    second = runtime.store.create()
    with serve_app(app) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url)
            expect(page.locator("#send")).to_be_enabled()
            page.locator("#history-panel").evaluate("(node) => node.open = true")
            held = []
            page.route(f"**/api/conversations/{first.id}/view", lambda route: held.append(route))
            page.locator(f'button[data-id="{first.id}"]').click()
            expect(page.locator("#conversation-status")).to_have_text("正在加载")
            page.locator(f'button[data-id="{second.id}"]').click()
            expect(page.locator(f'button[data-id="{second.id}"]')).to_have_attribute(
                "aria-current", "true",
            )
            page.wait_for_function(
                "(id) => localStorage.getItem('verification-conversation') === id",
                arg=second.id,
            )
            assert len(held) == 1
            held[0].continue_()
            page.wait_for_load_state("networkidle")
            expect(page.locator(f'button[data-id="{second.id}"]')).to_have_attribute(
                "aria-current", "true",
            )
            assert page.evaluate(
                "localStorage.getItem('verification-conversation')",
            ) == second.id
            assert errors == []
        finally:
            browser.close()
```

- [ ] **Step 2: Run the new contracts red**

```bash
uv run --no-sync python -m pytest tests/test_frontend_modules.py tests/test_distribution.py -q
uv run --no-sync python -m playwright install chromium --only-shell
RUN_BROWSER_TESTS=1 uv run --no-sync python -m pytest tests/test_frontend_modules_browser.py -q
```

Expected: FAIL for absent module assets, module script tag, exported interfaces,
and package module contents. The existing browser behavior is the regression
oracle, not a new screenshot or a redesign.

- [ ] **Step 3: Define state and pure transport explicitly**

`state.js`:

```javascript
export const statuses = {
  queued: "排队中",
  planning: "正在规划", waiting_for_user: "等待确认或补充", running: "执行中",
  completed: "已完成", failed: "失败", interrupted: "已中断",
};
export const nodeStatuses = {
  pending: "待执行", queued: "排队中", running: "运行中", completed: "已完成", failed: "失败",
  interrupted: "已中断",
};

export function createState() {
  return {
    token: "", current: null, pendingView: null, selection: 0, sending: false,
    timer: null, baselineKey: "", historyRequest: 0, directoryPicker: null,
    directoryRequest: 0, nativeDirectoryPickerEnabled: false, nativePicker: null,
    locations: [],
  };
}

export function active(state) {
  return state.pendingView !== null
    || Boolean(state.current && ["queued", "planning", "running"].includes(state.current.status));
}
```

`api.js`:

```javascript
export const API = "/api/conversations";

export function createApi(getToken) {
  async function response(path, options = {}) {
    const headers = {"X-Flight-Token": getToken()};
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    const result = await fetch(path, {
      method: options.method || "GET", headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    if (!result.ok) {
      let detail;
      try {
        const data = await result.json();
        detail = typeof data.detail === "string" ? data.detail
          : JSON.stringify(data.detail || data);
      } catch {
        detail = `HTTP ${result.status}`;
      }
      throw new Error(detail);
    }
    return result;
  }

  async function api(path, options) {
    return (await response(path, options)).json();
  }

  return {response, api};
}
```

`conversation()` stays in bootstrap because it updates pending-view state,
storage and submitted drafts; transport does not own those side effects.

- [ ] **Step 4: Extract all existing functions using the exact ownership map**

Move these named functions, preserving their bodies except for state access and
explicit factory wiring:

```text
render.js:
  element notice resourceLocation renderLocation renderProgress controls
  directoryControls table details messageText render

task-evidence.js:
  sourceText metricCard renderChecks renderEvaluation renderTask download renderDelivery

resize-preferences.js:
  initializeColumnResize

bootstrap.js:
  conversation closeDirectoryPicker resetDirectoryListing loadDirectory
  chooseNativeDirectory openDirectoryPicker chooseDirectory poll history
  clearConversation select send decide start
```

Every former mutable global in `State` becomes `state.FIELD`, including
increments, assignments, timer cancellation, stale-response comparisons and
template expressions. Keep record properties such as `bootstrap.token`,
`picker.selection`, `pendingView.selection`, and object property keys unchanged.
Expand shorthand `{target, selection, path: null}` to
`{target, selection: state.selection, path: null}`.

These are complete factory boundaries; insert the exact functions listed above
between each factory’s initial declarations and its return, rather than copying
another task’s version of them:

```javascript
// render.js
import {active as stateActive, statuses} from "./state.js";

export function createRenderer(state, {closeDirectoryPicker, renderTask}) {
  const $ = (id) => document.getElementById(id);
  const active = () => stateActive(state);
  return {
    $, element, notice, resourceLocation, renderLocation, renderProgress, controls,
    directoryControls, table, details, messageText, render,
  };
}
```

```javascript
// task-evidence.js
import {active as stateActive, nodeStatuses} from "./state.js";
import {API} from "./api.js";

export function createTaskEvidence(state, {$, element, notice, table, details, response}) {
  const active = () => stateActive(state);
  return {
    sourceText, metricCard, renderChecks, renderEvaluation, renderTask, download, renderDelivery,
  };
}
```

The following executable extraction script supplies those function bodies
directly from the **still-unsplit** `agent.js`. It includes a lexical rewrite
that skips strings, comments and property names and enters template expressions.
It is a one-time extraction command, not a new repository utility or dependency.
Run it only after writing `state.js` and `api.js` above and before replacing
`agent.js`, as standard input to `uv run --no-sync python -`.

```python
import re
from pathlib import Path
from textwrap import indent

static = Path("src/flight_agent/static")
source = (static / "agent.js").read_text("utf-8")
names = set(
    "token current pendingView selection sending timer baselineKey historyRequest "
    "directoryPicker directoryRequest nativeDirectoryPickerEnabled nativePicker locations".split()
)
matches = list(re.finditer(r"^(?:async )?function (\w+)\(", source, re.MULTILINE))
events_start = source.index('$("composer").addEventListener')
events_end = source.index("function initializeColumnResize(")
boundaries = sorted(
    {match.start() for match in matches} | {events_start, events_end, source.rindex("\nstart();")}
)
functions = {
    match.group(1): source[
        match.start():next(position for position in boundaries if position > match.start())
    ].strip()
    for match in matches
}


def rewrite(text):
    def walk(index, closing=False):
        output = []
        while index < len(text):
            char = text[index]
            if closing and char == "}":
                return "".join(output), index + 1
            if text.startswith("//", index):
                end = text.find("\n", index)
                end = len(text) if end == -1 else end
                output.append(text[index:end])
                index = end
            elif text.startswith("/*", index):
                end = text.index("*/", index + 2) + 2
                output.append(text[index:end])
                index = end
            elif char in ("'", '"'):
                start = index
                quote = char
                index += 1
                while index < len(text):
                    if text[index] == "\\":
                        index += 2
                    elif text[index] == quote:
                        index += 1
                        break
                    else:
                        index += 1
                output.append(text[start:index])
            elif char == "`":
                output.append("`")
                index += 1
                while index < len(text):
                    if text[index] == "\\":
                        output.append(text[index:index + 2])
                        index += 2
                    elif text[index] == "`":
                        output.append("`")
                        index += 1
                        break
                    elif text.startswith("${", index):
                        expression, index = walk(index + 2, True)
                        output.append("${" + expression + "}")
                    else:
                        output.append(text[index])
                        index += 1
            elif char == "{":
                body, index = walk(index + 1, True)
                output.append("{" + body + "}")
            elif re.match(r"[A-Za-z_$]", char):
                match = re.match(r"[A-Za-z_$][\w$]*", text[index:])
                word = match.group()
                before = text[:index].rstrip()[-1:]
                after = text[index + len(word):].lstrip()[:1]
                if word in names and before != "." and after != ":":
                    replacement = "state." + word
                    if before in ("{", ",") and after in (",", "}"):
                        replacement = word + ": " + replacement
                    output.append(replacement)
                else:
                    output.append(word)
                index += len(word)
            else:
                output.append(char)
                index += 1
        assert not closing, "Unclosed JavaScript block/template expression"
        return "".join(output), index

    return walk(0)[0]


groups = {
    "render.js": [
        "element", "notice", "resourceLocation", "renderLocation", "renderProgress",
        "controls", "directoryControls", "table", "details", "messageText", "render",
    ],
    "task-evidence.js": [
        "sourceText", "metricCard", "renderChecks", "renderEvaluation",
        "renderTask", "download", "renderDelivery",
    ],
    "bootstrap.js": [
        "conversation", "closeDirectoryPicker", "resetDirectoryListing", "loadDirectory",
        "chooseNativeDirectory", "openDirectoryPicker", "chooseDirectory", "poll",
        "history", "clearConversation", "select", "send", "decide", "start",
    ],
}
assigned = {name for group in groups.values() for name in group}
assert set(functions) == assigned | {"response", "api", "active", "initializeColumnResize"}
headers = {
    "render.js": (
        'import {active as stateActive, statuses} from "./state.js";\n\n'
        'export function createRenderer(state, {closeDirectoryPicker, renderTask}) {\n'
        '  const $ = (id) => document.getElementById(id);\n'
        '  const active = () => stateActive(state);\n'
    ),
    "task-evidence.js": (
        'import {active as stateActive, nodeStatuses} from "./state.js";\n'
        'import {API} from "./api.js";\n\n'
        'export function createTaskEvidence(state, {$, element, notice, table, details, response}) {\n'
        '  const active = () => stateActive(state);\n'
    ),
}
for filename in ("render.js", "task-evidence.js"):
    body = "\n\n".join(rewrite(functions[name]) for name in groups[filename])
    exports = (["$"] if filename == "render.js" else []) + groups[filename]
    text = headers[filename] + indent(body, "  ")
    text += "\n\n  return {" + ", ".join(exports) + "};\n}\n"
    (static / filename).write_text(text, "utf-8")

resize = functions["initializeColumnResize"]
(static / "resize-preferences.js").write_text(
    'const $ = (id) => document.getElementById(id);\n\nexport ' + resize + "\n", "utf-8",
)
bootstrap_header = '''import {API, createApi} from "./api.js";
import {active as stateActive, createState, statuses} from "./state.js";
import {createRenderer} from "./render.js";
import {createTaskEvidence} from "./task-evidence.js";
import {initializeColumnResize} from "./resize-preferences.js";

const state = createState();
const active = () => stateActive(state);
const {response, api} = createApi(() => state.token);
let taskEvidence;
const renderer = createRenderer(state, {
  closeDirectoryPicker: () => closeDirectoryPicker(),
  renderTask: (view) => taskEvidence.renderTask(view),
});
const {
  $, element, notice, resourceLocation, renderLocation, renderProgress, controls,
  directoryControls, render,
} = renderer;
taskEvidence = createTaskEvidence(state, {...renderer, response});

'''
body = "\n\n".join(
    rewrite(functions[name]) for name in groups["bootstrap.js"] if name != "start"
)
start = rewrite(functions["start"])
events = rewrite(source[events_start:events_end].strip())
start = start.replace(
    "async function start() {", "export async function start() {\n" + indent(events, "  "), 1,
)
(static / "bootstrap.js").write_text(bootstrap_header + body + "\n\n" + start + "\n", "utf-8")
(static / "agent.js").write_text('import {start} from "./bootstrap.js";\n\nstart();\n', "utf-8")
print("Extracted six native modules; no Node or build step")
```

The rewrite handles this source's syntax; it is not a general JavaScript parser.
Inspect the diff for shorthand object fields, regular-expression literals, and
`${...}` expressions. Do not “simplify” the existing race guards, promise
sequencing, consent logic, text escaping, resize calculations, or storage keys.
The browser tests below are mandatory execution evidence.

- [ ] **Step 5: Change HTML loading and the explicit static allowlist**

Replace the existing script element with:

```html
<script type="module" src="/static/agent.js"></script>
```

Do not add inline JavaScript, an import map, a CDN, `unsafe-inline`, `unsafe-eval`,
or a bundle. Add only these entries to `pages.py`:

```python
STATIC_ASSETS.update({
    "api.js": "text/javascript",
    "state.js": "text/javascript",
    "render.js": "text/javascript",
    "task-evidence.js": "text/javascript",
    "resize-preferences.js": "text/javascript",
    "bootstrap.js": "text/javascript",
})
```

The existing `flight_agent = ["static/*", "matlab_support/*.m"]` package-data
pattern already covers the flat module files; do not broaden it to arbitrary
repository files or add a recursive static mount.

- [ ] **Step 6: Verify module loading, race behavior, original UX and packaging**

```bash
uv run --no-sync python -m pytest tests/test_frontend_modules.py tests/test_distribution.py tests/test_web_surface.py tests/test_web_modules.py -q
RUN_BROWSER_TESTS=1 uv run --no-sync python -m pytest tests/test_frontend_modules_browser.py tests/test_agent_browser.py tests/test_agent_layout_browser.py tests/test_agent_column_resize_browser.py tests/test_matlab_project_browser.py -q
uv run --no-sync python -m ruff check .
uv run --no-sync python -m mypy src
```

Expected: PASS with all seven JavaScript URLs loaded by Chromium, no page errors,
no absent/incorrect-MIME imports, and no new consent or submission behavior.
Existing `test_accepted_submission_survives_view_failure_without_duplicate_post`
must still issue only one POST.
`test_queued_state_keeps_polling_and_disables_confirmation` from Stage 3 must
remain green. Existing source-text/XSS, archived-download,
responsive, keyboard-resize, corrupted-preference, focus and unsent-draft tests
must remain unchanged and pass. Wheel and sdist include every JS module, the
factory/routes and `.m` support files.

- [ ] **Step 7: Commit the native module extraction**

```bash
git add src/flight_agent/static/agent.html src/flight_agent/static/agent.js src/flight_agent/static/api.js src/flight_agent/static/state.js src/flight_agent/static/render.js src/flight_agent/static/task-evidence.js src/flight_agent/static/resize-preferences.js src/flight_agent/static/bootstrap.js src/flight_agent/web_routes/pages.py tests/test_frontend_modules.py tests/test_frontend_modules_browser.py tests/test_distribution.py
git commit -m "refactor: split workbench into native browser modules"
```

### Task 5: Add Software, Browser and Explicitly Gated MATLAB CI

**Files:**
- Create: `.github/workflows/ci.yml`.
- Modify: `pyproject.toml` — register `browser` beside the existing `matlab` marker.
- Modify: `tests/test_agent_browser.py:17-20`.
- Modify: `tests/test_agent_layout_browser.py:9-11`.
- Modify: `tests/test_agent_column_resize_browser.py:9-11`.
- Modify: `tests/test_matlab_project_browser.py:15-17`.
- Modify: `tests/test_frontend_modules_browser.py` — add browser marker.
- Modify: `tests/test_local_matlab_integration.py` — extend the existing final real linearization test.
- Create: `tests/test_ci_contract.py`.
- Modify: `uv.lock` only if marker/tool metadata makes `uv lock --check` require regeneration; do not upgrade dependencies.

**Interfaces:**
- Hosted `software` job: matrix Python `"3.11"`/`"3.12"`, strict locked sync,
  non-browser/non-MATLAB tests, Ruff, mypy and locked non-isolated package build.
- Hosted `browser` job: Python `"3.12"`, strict locked sync, installed Chromium
  and OS dependencies, `RUN_BROWSER_TESTS=1`, `-m "browser and not matlab"`.
- Manual `matlab` job: `workflow_dispatch.inputs.run_matlab` boolean, default
  `false`; default-branch dispatch only; `matlab-acceptance` environment;
  self-hosted labels `[self-hosted, matlab]`.
- Runner administrator provisions a Unix self-hosted runner with Bash and
  Actions runner version `2.329.0` or newer, plus repository
  variable `MATLAB_ACCEPTANCE_ENV`:
  an absolute path to a **dedicated virtual environment** containing the compatible,
  already-installed Engine. It is not a project source directory or the default
  development `.venv`. Its interpreter and MATLAB toolboxes/license are validated
  before synchronization. The job does not install or guess an Engine version.
- Only the licensed job uses `uv sync --locked --extra dev --inexact`, preserving
  the preinstalled, intentionally out-of-lock Engine. Hosted jobs use exact sync.
- Hosted cache owns only `/tmp/flight-agent-uv-cache`, never a
  virtual environment, user profile, MATLAB directory, project source or browser
  binary installation.

- [ ] **Step 1: Write failing workflow/marker contracts**

Create `tests/test_ci_contract.py`:

```python
import ast
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow():
    return yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text("utf-8"), Loader=yaml.BaseLoader,
    )


def runs(job):
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_hosted_ci_has_matrix_quality_gates_and_separate_browser_job():
    data = workflow()
    assert set(data["on"]) == {"push", "pull_request", "workflow_dispatch"}
    assert "pull_request_target" not in data["on"]
    assert data["permissions"] == {"contents": "read"}
    jobs = data["jobs"]
    assert set(jobs) == {"software", "browser", "matlab"}
    assert jobs["software"]["strategy"]["matrix"]["python-version"] == ["3.11", "3.12"]
    software = runs(jobs["software"])
    for command in (
        "uv lock --check", "uv sync --locked --extra dev",
        '-m "not matlab and not browser"', "python -m ruff check .",
        "python -m mypy src", "python -m build --no-isolation",
        "git diff --exit-code -- uv.lock",
    ):
        assert command in software
    browser = runs(jobs["browser"])
    assert "playwright install --with-deps chromium --only-shell" in browser
    assert '-m "browser and not matlab"' in browser
    assert jobs["browser"]["env"]["RUN_BROWSER_TESTS"] == "1"
    assert jobs["software"]["env"]["RUN_BROWSER_TESTS"] == "0"
    for name in ("software", "browser"):
        assert jobs[name]["env"]["RUN_MATLAB_TESTS"] == "0"
        cache = [
            step for step in jobs[name]["steps"]
            if step.get("uses", "").startswith("actions/cache@")
        ]
        assert len(cache) == 1
        assert cache[0]["with"]["path"] == "${{ env.UV_CACHE_DIR }}"
        assert "hashFiles('uv.lock')" in cache[0]["with"]["key"]
        for step in jobs[name]["steps"]:
            if step.get("uses", "").startswith("astral-sh/setup-uv@"):
                assert step["with"]["enable-cache"] == "false"
            if step.get("uses", "").startswith("actions/setup-python@"):
                assert "cache" not in step.get("with", {})


def test_matlab_requires_manual_trusted_licensed_runner_and_preserves_engine():
    data = workflow()
    option = data["on"]["workflow_dispatch"]["inputs"]["run_matlab"]
    assert option["type"] == "boolean"
    assert option["default"] == "false"
    job = data["jobs"]["matlab"]
    assert "github.event_name == 'workflow_dispatch'" in job["if"]
    assert "inputs.run_matlab" in job["if"]
    assert "github.event.repository.default_branch" in job["if"]
    assert job["runs-on"] == ["self-hosted", "matlab"]
    assert job["environment"] == "matlab-acceptance"
    assert job["env"]["RUN_MATLAB_TESTS"] == "1"
    assert job["env"]["MATLAB_ALLOW_TRUSTED_PROJECTS"] == "1"
    command = runs(job)
    assert "uv sync --locked --extra dev --inexact" in command
    assert "import matlab.engine" in command
    assert "test_local_matlab_integration.py" in command
    assert not any(
        step.get("uses", "").startswith("actions/cache@") for step in job["steps"]
    )


def test_every_browser_test_module_has_marker_and_opt_in_gate():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    markers = project["tool"]["pytest"]["ini_options"]["markers"]
    assert any(value.startswith("browser:") for value in markers)
    for path in (ROOT / "tests").glob("test_*browser*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "pytestmark"
                    for target in node.targets)
        )
        assert isinstance(assignment.value, ast.List), path.name
        marker = ast.unparse(assignment.value)
        assert "pytest.mark.browser" in marker
        assert "RUN_BROWSER_TESTS" in marker
```

- [ ] **Step 2: Run the workflow tests red**

```bash
uv run --no-sync python -m pytest tests/test_ci_contract.py -q
```

Expected: FAIL because the workflow is absent and browser markers are not yet
registered/applied.

- [ ] **Step 3: Register and apply a browser marker without removing opt-in gates**

Change pytest configuration to:

```toml
markers = [
    "matlab: requires an installed and licensed MATLAB with compatible Engine",
    "browser: requires RUN_BROWSER_TESTS=1 and installed Playwright Chromium",
]
```

In all five browser test modules, preserve the current environment gate and use:

```python
pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.environ.get("RUN_BROWSER_TESTS") != "1",
        reason="Browser tests are opt-in",
    ),
]
```

Keep MATLAB-marked functions and fake-Engine software tests in their existing
categories; do not mark whole local-MATLAB test modules merely by filename.

- [ ] **Step 4: Extend actual linearization acceptance to exercise `allmargin`**

In `test_actual_linearize_preparation_workspace_visibility`, after its existing
successful linearization/provenance/resume assertions, append:

```python
    from flight_agent.runtime.evidence import publish_evidence, verify_evidence
    from flight_agent.verification.matlab_contracts import AllmarginInput

    # The backend boundary consumes evidence relative to the next run's parent.
    # Publish exact producer bytes; do not invent a MAT model or producer metadata.
    published = []
    for filename, artifact_id in (
        ("linear-model.mat", "linear_model"), ("metadata.json", "metadata"),
    ):
        artifact = next(item for item in result.artifacts if item.path == filename)
        original = verify_evidence(directory, artifact)
        relative = f"calls/{request.id}/published/{filename}"
        publish_evidence(tmp_path, relative, original.read_bytes())
        descriptor = artifact.model_copy(update={
            "id": artifact_id, "node_id": "linearize", "path": relative,
        })
        verify_evidence(tmp_path, descriptor)
        published.append(descriptor)
    margins_request = LocalMatlabRequest(
        id="d" * 32,
        operation="allmargin",
        inputs=AllmarginInput(linear_model=published[0], conditions=conditions),
        input_evidence=published,
    )
    margins_directory = tmp_path / "margins"
    margins = actual_backend.execute(margins_request, margins_directory)
    assert margins.valid and margins.execution_status == "completed", margins.diagnostics
    assert "metrics" in margins.details
    assert {"linear-model.mat", "producer-metadata.json", "bode.png"} <= {
        item.path for item in margins.artifacts
    }
    for artifact in margins.artifacts:
        verify_evidence(margins_directory, artifact)
    assert actual_backend.execute(
        margins_request, margins_directory, resume=True,
    ) == margins
    assert discover_matlab_project(str(root)) == project
```

This extends a `matlab`-marked, `RUN_MATLAB_TESTS`-gated test; it is not new
production execution logic. Existing HTTP thermal execution/`stepinfo` acceptance
already verifies downloaded JSON/Word report hashes and source immutability.
Existing software frequency tests remain responsible for direct-dependency,
permissions, tampered-model, feedback and source-attribution rejection.

Before a licensed run, software-only collection must deselect this test, not
silently replace it with a fake backend:

```bash
uv run --no-sync python -m pytest tests/test_local_matlab_integration.py -m "not matlab" --collect-only -q
```

Expected: all tests in that module are deselected.

- [ ] **Step 5: Add the complete workflow**

Create `.github/workflows/ci.yml` with this content:

```yaml
name: CI

on:
  push:
  pull_request:
  workflow_dispatch:
    inputs:
      run_matlab:
        description: Run licensed MATLAB acceptance on the trusted self-hosted runner
        type: boolean
        required: false
        default: false

permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

env:
  UV_CACHE_DIR: /tmp/flight-agent-uv-cache
  UV_PYTHON_DOWNLOADS: never

jobs:
  software:
    name: Software Python ${{ matrix.python-version }}
    runs-on: ubuntu-latest
    timeout-minutes: 25
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.11", "3.12"]
    env:
      RUN_BROWSER_TESTS: "0"
      RUN_MATLAB_TESTS: "0"
      MATLAB_ALLOW_TRUSTED_PROJECTS: "0"
    steps:
      - uses: actions/checkout@v6
        with:
          persist-credentials: false
      - uses: actions/setup-python@v6
        with:
          python-version: ${{ matrix.python-version }}
      - uses: astral-sh/setup-uv@v7
        with:
          version: "0.12.6"
          enable-cache: false
      - name: Cache uv downloads only
        uses: actions/cache@v4
        with:
          path: ${{ env.UV_CACHE_DIR }}
          key: uv-${{ runner.os }}-${{ runner.arch }}-0.12.6-software-${{ matrix.python-version }}-${{ hashFiles('uv.lock') }}
      - name: Validate committed lock
        run: uv lock --check
      - name: Install locked development dependencies
        run: uv sync --locked --extra dev --python "${{ matrix.python-version }}"
      - name: Software tests
        run: uv run --no-sync python -m pytest -m "not matlab and not browser" -q
      - name: Ruff
        run: uv run --no-sync python -m ruff check .
      - name: Mypy
        run: uv run --no-sync python -m mypy src
      - name: Build wheel and source distribution
        run: uv run --no-sync python -m build --no-isolation
      - name: Lock must remain unchanged
        run: git diff --exit-code -- uv.lock

  browser:
    name: Browser Chromium
    runs-on: ubuntu-latest
    timeout-minutes: 20
    env:
      RUN_BROWSER_TESTS: "1"
      RUN_MATLAB_TESTS: "0"
      MATLAB_ALLOW_TRUSTED_PROJECTS: "0"
    steps:
      - uses: actions/checkout@v6
        with:
          persist-credentials: false
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - uses: astral-sh/setup-uv@v7
        with:
          version: "0.12.6"
          enable-cache: false
      - name: Cache uv downloads only
        uses: actions/cache@v4
        with:
          path: ${{ env.UV_CACHE_DIR }}
          key: uv-${{ runner.os }}-${{ runner.arch }}-0.12.6-browser-3.12-${{ hashFiles('uv.lock') }}
      - name: Validate committed lock
        run: uv lock --check
      - name: Install locked development dependencies
        run: uv sync --locked --extra dev --python "3.12"
      - name: Install Chromium and Linux dependencies
        run: uv run --no-sync python -m playwright install --with-deps chromium --only-shell
      - name: Browser tests
        run: uv run --no-sync python -m pytest -m "browser and not matlab" -q
      - name: Lock must remain unchanged
        run: git diff --exit-code -- uv.lock

  matlab:
    name: Licensed MATLAB acceptance
    if: >-
      github.event_name == 'workflow_dispatch' && inputs.run_matlab &&
      github.ref == format('refs/heads/{0}', github.event.repository.default_branch)
    runs-on: [self-hosted, matlab]
    environment: matlab-acceptance
    timeout-minutes: 60
    env:
      RUN_BROWSER_TESTS: "0"
      RUN_MATLAB_TESTS: "1"
      MATLAB_ALLOW_TRUSTED_PROJECTS: "1"
      UV_PROJECT_ENVIRONMENT: ${{ vars.MATLAB_ACCEPTANCE_ENV }}
      MATLAB_PYTHON_EXECUTABLE: ${{ format('{0}/bin/python', vars.MATLAB_ACCEPTANCE_ENV) }}
    steps:
      - uses: actions/checkout@v6
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@v7
        with:
          version: "0.12.6"
          enable-cache: false
      - name: Validate dedicated compatible Engine environment
        shell: bash
        run: |
          test -n "$UV_PROJECT_ENVIRONMENT"
          test "${UV_PROJECT_ENVIRONMENT:0:1}" = "/"
          test -f "$UV_PROJECT_ENVIRONMENT/pyvenv.cfg"
          test "$UV_PROJECT_ENVIRONMENT" != "$GITHUB_WORKSPACE/.venv"
          test -x "$MATLAB_PYTHON_EXECUTABLE"
          "$MATLAB_PYTHON_EXECUTABLE" -c 'import sys; import matlab.engine; assert sys.version_info >= (3, 11); print(sys.executable)'
      - name: Validate committed lock
        run: uv lock --check
      - name: Install locked tools without removing the preinstalled Engine
        shell: bash
        run: uv sync --locked --extra dev --inexact --python "$MATLAB_PYTHON_EXECUTABLE"
      - name: Check actual MATLAB and required licenses
        shell: bash
        run: |
          "$MATLAB_PYTHON_EXECUTABLE" - <<'PY'
          import matlab.engine
          engine = matlab.engine.start_matlab("-nodesktop -nosplash")
          try:
              for feature in (
                  "MATLAB", "SIMULINK", "Control_Toolbox", "Simulink_Control_Design",
              ):
                  assert engine.license("test", feature), feature
              print(engine.version())
          finally:
              engine.quit()
          PY
      - name: Real project stepinfo linearization allmargin and evidence acceptance
        run: >-
          uv run --no-sync python -m pytest
          tests/test_verification_matlab.py
          tests/test_local_matlab.py
          tests/test_local_matlab_integration.py
          -m matlab -v -s
      - name: Lock must remain unchanged
        run: git diff --exit-code -- uv.lock
```

No hosted job receives MATLAB/model credentials. Do not add automatic Engine
installation, an external Worker, Copilot login, an inferred model fallback,
`continue-on-error`, `--frozen` in place of freshness checks, or a cache for
`.venv`, the dedicated Engine environment, MATLAB, or Playwright browsers.
The manually run self-hosted job never runs fork PR code. Configure
`matlab-acceptance` with required reviewers before enabling it.

- [ ] **Step 6: Verify workflow semantics and exact test selection**

```bash
uv lock --check
uv run --no-sync python -m pytest tests/test_ci_contract.py tests/test_reproducibility.py -q
RUN_BROWSER_TESTS=0 RUN_MATLAB_TESTS=0 uv run --no-sync python -m pytest -m "not matlab and not browser" --strict-markers -q
RUN_BROWSER_TESTS=1 RUN_MATLAB_TESTS=0 uv run --no-sync python -m pytest -m "browser and not matlab" --strict-markers -q
uv run --no-sync python -m ruff check .
uv run --no-sync python -m mypy src
uv run --no-sync python -m build --no-isolation
git --no-pager diff --exit-code -- uv.lock
```

Expected: PASS. Software selection launches no browser or real MATLAB process;
browser selection runs the five marked browser modules, not a silently skipped
suite; no unknown markers or import failures. Investigate every unexpected skip.
The parsed workflow tests catch absent matrix jobs, missing quality gates,
unmarked browser modules, a broadened manual gate and the wrong cached path.

Actual GitHub dispatch/runner execution is separate verification after pushing;
passing local YAML contract tests is not a claim that hosted Actions have run.

- [ ] **Step 7: Commit CI and its executable contracts**

```bash
git add .github/workflows/ci.yml pyproject.toml tests/test_ci_contract.py tests/test_agent_browser.py tests/test_agent_layout_browser.py tests/test_agent_column_resize_browser.py tests/test_matlab_project_browser.py tests/test_frontend_modules_browser.py tests/test_local_matlab_integration.py
git commit -m "ci: verify software browser and opt-in licensed MATLAB paths"
```

If a metadata-only lock regeneration was required, include `uv.lock` in this
commit after verifying its package versions have not changed.

### Task 6: Update Focused Architecture/Operations Docs and Run the Stage Gate

**Files:**
- Modify: `README.md` — development commands/module table only.
- Modify: `docs/usage.md` — dedicated licensed-environment procedure only.
- Modify: `docs/architecture.md` — web/module ownership, native frontend, current verification procedure.
- Test: `tests/test_reproducibility.py`, `tests/test_ci_contract.py`, complete software/browser suites.

**Interfaces:**
- Consumes: factory/router/module boundaries from Tasks 2–4, markers/workflow and
  `MATLAB_ACCEPTANCE_ENV` runner contract from Task 5.
- Produces: copy/paste bootstrap and verification instructions that do not
  accidentally exact-sync an Engine environment.
- Preserves: the Stages 2–5 documentation of SQLite WAL, bounded scheduling,
  immutable snapshots, context metrics, and redacted diagnostics.

- [ ] **Step 1: Extend the docs contract before editing documentation**

Append to `tests/test_reproducibility.py`:

```python
def test_docs_describe_actual_modules_jobs_and_engine_opt_in():
    architecture = (ROOT / "docs/architecture.md").read_text("utf-8")
    for name in (
        "application.py", "web_routes/", "api.js", "state.js", "render.js",
        "task-evidence.js", "resize-preferences.js", "bootstrap.js",
        'not matlab and not browser', 'browser and not matlab',
        "uv run --no-sync python -m build --no-isolation",
        "MATLAB_ACCEPTANCE_ENV", "--inexact", "RUN_MATLAB_TESTS=1",
    ):
        assert name in architecture
    usage = (ROOT / "docs/usage.md").read_text("utf-8")
    assert "MATLAB_ACCEPTANCE_ENV" in usage
    assert "--inexact" in usage
    assert "RUN_MATLAB_TESTS=1" in usage
    readme = (ROOT / "README.md").read_text("utf-8")
    assert 'not matlab and not browser' in readme
    assert "application.py" in readme
```

- [ ] **Step 2: Observe missing operational/module documentation**

```bash
uv run --no-sync python -m pytest tests/test_reproducibility.py -q
```

Expected: the new docs contract fails while the Task 1 lock/bootstrap checks pass.

- [ ] **Step 3: Document exactly the implemented composition and native modules**

Replace the README module table’s web entry and corresponding architecture
source-tree lines with:

```text
web.py                     Web 组合入口、同源/令牌校验、安全响应头与生命周期
application.py             运行时和计算后端装配；显式运行时/后台调度器注入
verification/routes.py     通用会话提交、确认、恢复、重试和状态投影
web_routes/                工件下载、能力/资源元数据、配置/目录、页面与静态资源
static/agent.js             稳定 URL 的原生 ES module 入口
static/api.js               API 传输与令牌头
static/state.js             单一会话/UI 状态
static/render.js            会话、进度与控件的文本 DOM 渲染
static/task-evidence.js     基线、授权、评价、证据与当前/归档任务下载
static/resize-preferences.js 三栏调整、键盘交互与本地偏好
static/bootstrap.js         初始化、事件、轮询、历史、目录与请求时序
```

Add this architecture paragraph:

```text
create_web_app 保持原有公开入口、scheduler_settings 参数与 HTTP 路径，仅做 Web 组合。
application.py 负责默认运行时装配；测试可按对象身份注入 EngineeringRuntime，
也可通过 conversation_manager 注入包含 planning/execution 调度器的 ConversationManager。
外部注入的 manager 和 store 由调用者关闭，默认 manager 在应用退出时排空并关闭，
随后关闭应用拥有的 store；app.state.conversation_manager 保持可用。
路由依旧通过原有同源、Host、令牌、请求体大小和严格 schema 校验。
下载依旧校验任务目录身份、工件登记和 SHA-256，不直接暴露任意文件目录。
前端由浏览器直接加载原生 ES modules，不需要 Node、npm、打包器或编译。
模块 URL 使用显式白名单和 JavaScript MIME；CSP 不增加 unsafe-inline 或 unsafe-eval。
```

Do not reintroduce the old documentation claims “no SQLite” or “one global
background task.” Keep the earlier stages’ more precise replacement text.

- [ ] **Step 4: Document software/browser and licensed-MATLAB verification separately**

Use this complete development block in README and architecture:

```bash
uv sync --extra dev
uv lock --check
RUN_BROWSER_TESTS=0 RUN_MATLAB_TESTS=0 uv run --no-sync python -m pytest -m "not matlab and not browser" -q
uv run --no-sync python -m ruff check .
uv run --no-sync python -m mypy src
uv run --no-sync python -m build --no-isolation
```

Use this browser block in architecture:

```bash
uv run --no-sync python -m playwright install chromium --only-shell
RUN_BROWSER_TESTS=1 RUN_MATLAB_TESTS=0 uv run --no-sync python -m pytest -m "browser and not matlab" -q
```

For Linux CI, document that the workflow installs Chromium with
`--with-deps`; tests launch their own ephemeral local app through `serve_app`,
not a detached dev server.

In both usage and architecture, document the licensed environment as an
administrator-provisioned input, then give this shell procedure. The variable is
required, not an example path to paste literally:

```bash
test -n "$MATLAB_ACCEPTANCE_ENV"
test -f "$MATLAB_ACCEPTANCE_ENV/pyvenv.cfg"
test -x "$MATLAB_ACCEPTANCE_ENV/bin/python"
export UV_PROJECT_ENVIRONMENT="$MATLAB_ACCEPTANCE_ENV"
export MATLAB_PYTHON_EXECUTABLE="$MATLAB_ACCEPTANCE_ENV/bin/python"
"$MATLAB_PYTHON_EXECUTABLE" -c 'import matlab.engine; print(matlab.engine.__file__)'
uv sync --locked --extra dev --inexact --python "$MATLAB_PYTHON_EXECUTABLE"
RUN_MATLAB_TESTS=1 MATLAB_ALLOW_TRUSTED_PROJECTS=1 \
uv run --no-sync python -m pytest \
  tests/test_verification_matlab.py tests/test_local_matlab.py \
  tests/test_local_matlab_integration.py -m matlab -v -s
```

Add these operational facts:

```text
MATLAB_ACCEPTANCE_ENV 是专用于验收的兼容 Python 虚拟环境，须预装匹配发行版的
Engine，并拥有 MATLAB、Simulink、Control System Toolbox 和 Simulink Control Design
许可。--inexact 仅用于保留该环境中的非锁定 Engine，不用于常规软件 CI。
真实 MATLAB 验收不会回退到 fake Engine；未显式启用的 matlab 测试会跳过，
跳过不代表真实验收通过。必须记录 MATLAB 发行版、解释器、测试结果与失败原因。
GitHub CI 的软件矩阵为 Python 3.11/3.12，浏览器是单独的 Chromium 作业。
真实 MATLAB 作业仅能从默认分支手动勾选 run_matlab，使用 [self-hosted, matlab]
和受审批保护的 matlab-acceptance 环境；仓库变量 MATLAB_ACCEPTANCE_ENV 指向
该 runner 的专用环境。PR 不触发真实 MATLAB，且不会把 Engine 放入缓存。
缓存仅是 uv 下载缓存，不是虚拟环境、浏览器、MATLAB 安装或证据产物。
```

Keep the existing Python/MATLAB compatibility warning and existing Engine
installation guidance. Do not advertise a new generic `pip install matlabengine`
command.

- [ ] **Step 5: Run the complete software gate on each supported CI interpreter**

Use temporary environments outside the repository to keep local validation
environments out of the source tree:

```bash
CHECK_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/flight-agent-stage6.XXXXXX")"
export UV_PROJECT_ENVIRONMENT="$CHECK_ROOT/python311"
uv sync --locked --extra dev --python 3.11
RUN_BROWSER_TESTS=0 RUN_MATLAB_TESTS=0 uv run --no-sync python -m pytest -m "not matlab and not browser" -q
uv run --no-sync python -m ruff check .
uv run --no-sync python -m mypy src
uv run --no-sync python -m build --no-isolation
export UV_PROJECT_ENVIRONMENT="$CHECK_ROOT/python312"
uv sync --locked --extra dev --python 3.12
RUN_BROWSER_TESTS=0 RUN_MATLAB_TESTS=0 uv run --no-sync python -m pytest -m "not matlab and not browser" -q
uv run --no-sync python -m ruff check .
uv run --no-sync python -m mypy src
uv run --no-sync python -m build --no-isolation
uv run --no-sync python -m playwright install chromium --only-shell
RUN_BROWSER_TESTS=1 RUN_MATLAB_TESTS=0 uv run --no-sync python -m pytest -m "browser and not matlab" -q
unset UV_PROJECT_ENVIRONMENT
printf 'Temporary validation environments: %s\n' "$CHECK_ROOT"
uv lock --check
git --no-pager diff --check
git --no-pager diff --exit-code -- uv.lock
```

Expected: PASS on 3.11 and 3.12, plus Chromium browser PASS and a clean lock diff.
If an interpreter cannot be installed or obtained locally, explicitly report
that matrix leg unverified; use its hosted job as the pending gate. Do not claim
both versions passed from a single interpreter run.

The original Stage 2–5 tests remain in the full software selection. No Stage 6
change may weaken the WAL/restart, interrupted ledger, same-conversation
serialization, independent planning, local single-slot, snapshot tampering,
context limits, diagnostics redaction, confirmation, source, resource, receipt,
budget or artifact tests.

- [ ] **Step 6: Record licensed acceptance separately, only when provisioned**

Run the documented licensed command only in the dedicated environment. Expected:

- Actual local project execution plus `stepinfo` passes the existing thermal HTTP
  acceptance, including downloaded report/hash assertions.
- Actual linearization plus `allmargin` passes both workspace-preparation cases.
- Source inventories/fingerprints remain unchanged.
- Every produced frequency artifact verifies against its recorded hash.
- No fake MATLAB release/version is accepted as actual execution.

If no licensed environment is available, record “real MATLAB acceptance not run;
opt-in licensed gate pending,” not “all acceptance passed.” The software/browser
deliverable is still reviewable independently.

- [ ] **Step 7: Commit the focused documentation and stage-gate contract**

```bash
git add README.md docs/usage.md docs/architecture.md tests/test_reproducibility.py
git commit -m "docs: describe locked development CI and modular web boundaries"
git --no-pager status --short
```

Expected: only intended Stage 6 commits/files are present; no temporary
environment, browser profile, cache, Engine, source project, report or credential
is staged. Tasks 1–6 are ordered Stage 6 commits, not new delivery stages.

## Completion Evidence and Self-Review

Before reporting implementation complete, record actual command results rather
than expected results from this plan:

- [ ] `uv.lock` is generated, tracked and fresh; both matrix interpreters use
  `--locked --extra dev`; build dependencies come from the same lock.
- [ ] Factory tests prove exact runtime/resource/manager/scheduler identity and
  borrowed manager/store ownership; Stage 2–5 construction and lifecycle behavior
  is retained.
- [ ] Route manifest, auth/origin/Host/body/schema tests, native-picker tests and
  current/archived artifact integrity tests pass with unchanged URLs.
- [ ] All six native modules load from the wheel’s flat static directory through
  the explicit allowlist; no frontend build tools or CSP relaxation were added.
- [ ] Original browser regressions and new state/import/stale-response tests pass;
  no UI copy, layout, consent, storage key or payload changed.
- [ ] Parsed CI tests verify separate software/browser jobs, 3.11/3.12 quality
  gates, explicit real-MATLAB dispatch and download-only cache ownership.
- [ ] Documentation names actual files, signatures, commands and operational
  prerequisites; no contradictory pre-SQLite/global-lock claims are restored.
- [ ] Real MATLAB is reported as actual PASS/FAIL or explicitly not run, never
  inferred from a fake backend, import probe, skipped test or software PASS.
- [ ] Each task has a failing test, observed RED, minimal change, observed GREEN
  and its focused commit; final `git diff --check` and lock drift check pass.

Plan-author self-review scope: approved Stage 6 requirements map to Tasks 1–6;
Stages 2–5 are prerequisites rather than duplicate implementation; all new
interfaces and module owners are specified; implementation and commits remain
future work. No implementation verification result is asserted by saving this
document.
