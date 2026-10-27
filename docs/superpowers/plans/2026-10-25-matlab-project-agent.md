# Generic Local MATLAB Project Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 输入已准备工程目录、分析要求及评价指标，在确认后通过本机 MATLAB 执行已有入口，并交付可追溯的实测结论。

**Architecture:** 复用 v2 Agent 的规划、确认、依赖图、证据和评价。新增静态工程发现与本机子进程后端，将工程执行和 stepinfo 分为两个通过不可变时序工件连接的能力；新路径不依赖 F-16 布局、配平点 1 或专用登记文件。

**Tech Stack:** Python 3.11+、Pydantic、FastAPI、MATLAB Engine、现有 HTML/CSS/JavaScript、pytest、Playwright、Ruff、mypy；不新增第三方依赖。

**Spec:** `docs/superpowers/specs/2026-10-25-matlab-project-agent-design.md`

## Global Constraints

- 工程提供者保证模型和已有初始化、分析入口本身可运行。
- Agent 识别并调用已有入口；缺少必要配置或分析脚本时向用户澄清。
- 不生成分析脚本，不修改原模型，不自动补建激励、日志或控制器。
- 已由工程明确提供的设置应直接沿用，不要求用户重复填写。
- 保留既有基线确认、严格能力契约、不可变证据、恢复及历史兼容机制。
- 发现阶段只读取受限的本地文件和静态元数据。
- 节点之间只通过可验证的序列化输出和工件传递数据，不依赖隐藏的共享 base workspace。
- 没有可验证终态时停止为未知执行状态，不能自动重发。
- 真实用户工程必须由用户明确提供目录、必要的已有入口和人工参考结果。
- 在当前目录和分支实施，不建立 worktree；不自动推送，不重启既有用户服务。
- 本机可信工程执行沿用 `MATLAB_ALLOW_TRUSTED_PROJECTS=1` 部署开关及当前基线授权，不能借新后端绕过原有权限。
- 配置 `MATLAB_PYTHON_EXECUTABLE` 时使用其指定的已有本机解释器，否则使用当前解释器；探测和运行均使用同一个选择。
- 每个任务独立经历 RED/GREEN 和最小覆盖检查；提交加入本会话要求的两个 trailer。

## File Map and Shared Interfaces

| 文件 | 职责 |
| --- | --- |
| `verification/matlab_contracts.py` | 静态工程描述、已有入口、信号选择、本机请求/结果及 stepinfo 输入契约 |
| `verification/matlab_projects.py` | 有界静态发现、配置摘要、工程指纹和副本 |
| `verification/local_matlab.py` | 本机解释器探测、子进程生命周期、状态记录和原 ID 对账 |
| `verification/local_matlab_worker.py` | MATLAB Engine 生命周期及对打包支持函数的固定调用 |
| `verification/adapters/local_matlab.py` | 执行已有工程与 stepinfo 的能力封装、依赖验证和统一观测 |
| `matlab_support/flight_agent_run_project.m` | 执行已有入口/模型并导出明确选择的时序 |
| `matlab_support/flight_agent_local_stepinfo.m` | 读取导出的时序、调用指标函数并保存结果和曲线 |
| 现有 `projects.py`、`plugins.py`、`web.py`、`agent_planner.py` | 复用快照基础机制，装配本机资源和能力、提供规划上下文 |
| 现有 `routes.py`、`cli.py`、`static/agent.*` | 三项输入、原文保存、默认本机资源及旧客户端兼容 |
| 新增 `tests/test_matlab_projects.py` 等对应测试 | 分层回归与端到端验收，不在生产中加入模拟结果 |

以下名称是本计划各任务之间的约定，实施中若必须调整，先同步所有直接消费者和测试：

- `MatlabEntry`：`path` 相对路径，`kind` 为 script/function，`arguments` 为有限 JSON 列表。
- `MatlabProjectDescriptor`：`root`、`fingerprint`、`files`、`models`、`entries`、`project_files`、`notes`、`settings`。
- `SignalSelection`：`source` 为 logsout/workspace/return，`path` 为显式字段名列表，`unit`；不接受表达式。
- `ProjectExecutionInput`：`mode` 为 simulink/entry，`model`、`entry`、`initializer`、`project_file`、`working_directory`、`input_signal`、`response_signal`、`conditions`。
- `StepinfoInput`：`series: OutputArtifact`、`settling_band`、`steady_state: SteadyStatePolicy`、`conditions`。
- `LocalMatlabRequest`：原 `id`、`operation` 为 execute_project/stepinfo、严格 `inputs`、可选 `project: MatlabProjectDescriptor`、输入证据描述及 `timeout_seconds`。
- `LocalMatlabResult`：原 `id`、`request_sha256`、`execution_status`、`valid`、`details`、`diagnostics` 及 `artifacts`。
- `discover_matlab_project(root: str) -> MatlabProjectDescriptor`；`snapshot_matlab_project(project, destination: Path) -> Path`。
- `LocalMatlabBackend.from_environment()`；`availability() -> Availability`；`execute(request, directory: Path, *, resume: bool = False) -> LocalMatlabResult`。
- `local_matlab_capabilities(backend) -> list[Capability]`：注册名称为 `matlab_execute_project` 和 `matlab_stepinfo`。
- 执行节点的 `CapabilityOutcome.details["series"]` 是已发布 `OutputArtifact` 的 JSON 描述；stepinfo 的 `series` 只能绑定这一真实依赖输出。
- 规范化 `series.json` 含 input/response 两项，各自提供 time、values、time_unit、unit、name；不进入 LLM 上下文的原始数组。

---

### Task 1: Static Project Discovery and Explicit Entry Contracts

**Files:** Create `src/flight_agent/verification/matlab_contracts.py`, `src/flight_agent/verification/matlab_projects.py`, `tests/matlab_project_helpers.py`, `tests/test_matlab_projects.py`. Modify `src/flight_agent/verification/projects.py` only to share the existing bounded file inventory where needed.

**Consumes:** `FileDigest`, `relative_path`, `matlab_identifier`, `SteadyStatePolicy`, `safe_directory`, existing snapshot limits.

**Produces:** All shared contracts above, `discover_matlab_project`, `snapshot_matlab_project`, and test helper `prepared_project(root: Path, *, model_name: str = "plant", signal_name: str = "response") -> Path`.

- [ ] **Step 1: Write RED tests.** `prepared_project` creates `initialize.m`, a named model fixture, and README descriptions; no专用清单。Test two layouts, MATLAB-only `.m` entry, `.prj`/`.mdl` discovery, competing candidates, invalid paths, symlinks, bounds, changed resources and unchanged source files.

```python
def test_project_without_manifest_is_discovered_without_execution(tmp_path, monkeypatch):
    from flight_agent.verification.matlab_projects import discover_matlab_project
    from tests.matlab_project_helpers import prepared_project
    import subprocess
    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery must not execute a process")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    root = prepared_project(tmp_path / "case", model_name="pitch")
    project = discover_matlab_project(str(root))
    assert "pitch.slx" in project.models
    assert any(entry.path == "initialize.m" for entry in project.entries)
    assert len(project.fingerprint) == 64
```

- [ ] **Step 2: Run RED.** `.venv/bin/python -m pytest tests/test_matlab_projects.py -q`；确认失败原因是新增模块/行为未实现。
- [ ] **Step 3: Implement contracts and static discovery.** Parse only bounded existing files; retain multiple candidates rather than selecting the first. Share file inventory instead of running legacy F-16 profile inference. Snapshot must verify the full inventory before/after copying and contain no symlinks.

```python
model_files = [item.path for item in files if Path(item.path).suffix.lower() in {".slx", ".mdl"}]
project_files = [item.path for item in files if Path(item.path).suffix.lower() == ".prj"]
identity = {"files": [item.model_dump(mode="json") for item in files], "settings": settings}
project_fingerprint = fingerprint(identity)
```

- [ ] **Step 4: Run GREEN.** `.venv/bin/python -m pytest tests/test_matlab_projects.py tests/test_verification_projects.py -q`；两套工程均不调用 MATLAB，旧发现和快照行为不变。
- [ ] **Step 5: Commit.** `feat: discover prepared MATLAB projects without a manifest`；只暂存本任务文件并包含 required trailers。

### Task 2: Local MATLAB Backend, Existing Entry Execution and Durable State

**Files:** Create `src/flight_agent/verification/local_matlab.py`, `src/flight_agent/verification/local_matlab_worker.py`, `src/flight_agent/matlab_support/flight_agent_run_project.m`, `tests/test_local_matlab.py`, `tests/fixtures/fake_local_matlab/matlab/__init__.py`, `tests/fixtures/fake_local_matlab/matlab/engine.py`. Extend `matlab_contracts.py` and `tests/matlab_project_helpers.py` as required by the defined contracts.

**Consumes:** Task 1 descriptor/snapshot, existing `_stop_worker`, JSON writing, artifact digests and trusted-project enable flag.

**Produces:** `LocalMatlabBackend` and execute_project operation; a final validated `LocalMatlabResult` plus raw MAT, normalized JSON, log and execution metadata.

- [ ] **Step 1: Write RED tests.** Cover configured interpreter, unavailable interpreter/Engine, no Engine startup during probe, both entry and model modes, exact call identity, timeout, malformed/oversized output, known failure, lost receipt and resume without new execution. The fake engine exists only under tests and records invocation counts.

```python
def test_resume_does_not_start_a_second_engine(local_backend, local_request, tmp_path):
    directory = tmp_path / "run"
    first = local_backend.execute(local_request, directory)
    second = local_backend.execute(local_request, directory, resume=True)
    assert second == first
    assert (directory / "engine-starts.txt").read_text() == "1"
```

`local_backend` and `local_request` are fixtures defined in `tests/test_local_matlab.py`, using the Task 1 prepared project and the isolated fake Engine directory.

- [ ] **Step 2: Run RED.** `.venv/bin/python -m pytest tests/test_local_matlab.py -q`；确认缺失后端行为，而不是测试设置错误。
- [ ] **Step 3: Implement one-request/one-subprocess execution.** Persist request digest and running state before dispatch. The child imports Engine, starts MATLAB, invokes packaged support via `feval`, then quits. Scrub LLM/worker credentials as current project execution does. Preserve source directory and process ownership.

```python
environment = {
    key: value for key, value in os.environ.items()
    if not key.startswith(("LLM_", "OPENAI_", "AZURE_OPENAI_", "LANGSMITH_", "MATLAB_WORKER_"))
}
command = [interpreter, "-u", "-m", "flight_agent.verification.local_matlab_worker", str(directory)]
```

The packaged bridge receives only the request file path, not model-generated source. It opens the confirmed `.prj` if provided, adds confirmed project paths, executes the chosen initializer, and runs the existing entry or `sim`. Before invoking a function, verify its resolved file matches the selected snapshot entry rather than another same-named file on the path. It exports selected timeseries or explicit time/values structures to JSON and MAT. Access nested fields programmatically, never evaluate user expressions. Resume reads verified terminal state only; uncertain calls remain uncertain.

- [ ] **Step 4: Run GREEN.** `.venv/bin/python -m pytest tests/test_local_matlab.py tests/test_matlab.py tests/test_verification_matlab.py -q`；不请求真实 MATLAB，旧进程清理回归保持通过。
- [ ] **Step 5: Commit.** `feat: execute prepared projects with a local MATLAB backend`，只包含本任务文件和 required trailers。

### Task 3: Composable Project Execution and Parameterized stepinfo Capabilities

**Files:** Create `src/flight_agent/verification/adapters/local_matlab.py`, `src/flight_agent/matlab_support/flight_agent_local_stepinfo.m`, `tests/test_local_matlab_adapters.py`. Modify `matlab_support/flight_agent_step_metrics.m`, `verification/local_matlab_worker.py`, fake Engine fixture, and shared request/result contracts.

**Consumes:** Tasks 1–2, `AgentContext`, immutable evidence helpers, declared outcomes and `BudgetAccount`.

**Produces:** `local_matlab_capabilities`, exact tool names `matlab_execute_project` / `matlab_stepinfo`, and verified series artifact binding.

- [ ] **Step 1: Write RED tests.** Cover exact entry/resource binding, changed project and version, dependency artifact identity, local receipt recovery, explicit band 0.05 and 0.02, metric units/conditions and invalid evidence. Declared results cannot come from input numeric arrays or forged files.

```python
def test_stepinfo_band_and_source_are_forwarded(local_adapter_case):
    context, inputs, backend, tools = local_adapter_case
    tools.invoke("matlab_stepinfo", inputs, context, context.scope)
    request = backend.requests[-1]
    assert request.operation == "stepinfo"
    assert request.inputs["settling_band"] == 0.05
    assert request.inputs["series"]["sha256"] == inputs["series"]["sha256"]
```

`local_adapter_case` is a fixture defined in the new test module; it publishes a real test artifact and provides an in-memory backend recording requests, not a production fallback.

- [ ] **Step 2: Run RED.** `.venv/bin/python -m pytest tests/test_local_matlab_adapters.py -q`。
- [ ] **Step 3: Implement the two capabilities.** Project execution requires execute_project permission and its bound local resource. stepinfo verifies its series descriptor equals a direct dependency artifact, including producer identity, then verifies bytes before/after MATLAB calculation. Pin request ID/checkpoint before calling the backend; translate unknown vs known failures consistently with the existing Simulink adapter.

The MATLAB metric helper gains an optional settling-band argument; omitted argument preserves 0.05 for historical callers:

```matlab
function result = flight_agent_step_metrics(inputTime,inputValue,responseTime,responseValue,policy,settlingBand)
if nargin < 6
    settlingBand = 0.05;
end
info = stepinfo(y(after),elapsed,final,initial, ...
    'SettlingTimeThreshold',settlingBand);
```

Use that same argument in recorded details and plotted error bands. Validate finite `0 < settling_band < 1`. Publish uniform overshoot/settling_time observations only when the helper marks evidence valid; retain invalid diagnostics, series and plots otherwise. Parent adapter invokes the original run ID with resume=true for reconciliation and never dispatches unknown work again.

- [ ] **Step 4: Run GREEN.** `.venv/bin/python -m pytest tests/test_local_matlab_adapters.py tests/test_agent_adapters.py tests/test_agent_evaluation.py tests/test_verification_matlab.py -q`。
- [ ] **Step 5: Commit.** `feat: compose local project execution with parameterized stepinfo`，包含 required trailers。

### Task 4: Resource Registration, Planning and Confirmation Wiring

**Files:** Modify `verification/plugins.py`, `web.py`, `verification/agent_planner.py`, `verification/coordinator.py` only where required for saved clarification context. Create `tests/test_local_matlab_planning.py`.

**Consumes:** New descriptor, backend and capabilities; existing resource registry, AgentPlanner and AgentExecutor.

**Produces:** Catalog entries for local project discovery and execution, default application assembly without a Worker dependency, exact source/entry validation before confirmation.

- [ ] **Step 1: Write RED tests.** Assemble the real Web runtime with an injected controlled planner and local backend. Check candidate provenance, no manifest, multiple candidates leading to clarification, retained follow-up selections, unavailable local Engine as a capability gap, zero execution before confirmation, and four-node execute/stepinfo/evaluate/delivery plans.

```python
def test_local_resource_does_not_require_worker(local_planning_case):
    runtime, resource, backend = local_planning_case
    catalog = runtime.executor.tools.catalog((resource,))
    assert {item["name"] for item in catalog} >= {
        "matlab_execute_project", "matlab_stepinfo", "evaluate_criteria", "deliver_results",
    }
    assert resource.location == "agent"
    assert backend.requests == []
```

`local_planning_case` is defined in the new test module using Task 1 project fixtures and Task 2 backend interface.

- [ ] **Step 2: Run RED.** `.venv/bin/python -m pytest tests/test_local_matlab_planning.py -q`。
- [ ] **Step 3: Register and wire local resources/tools.** Resource discovery remains available without Engine; tool readiness uses bounded backend probe and deployment consent. Expose candidate paths relative to resource root with provenance; actual locators remain server-bound/redacted. Validate selected paths, signatures and series output bindings against the confirmed resource. Existing Worker tools and old aliases retain their meaning.

```python
ResourceAdapter(
    name="matlab_project", kind="matlab_project", location="agent",
    inspect=inspect_local_project, verify=verify_local_project,
    browse=browse_directories, display_name="本机 MATLAB / Simulink 工程", local=True,
)
```

Planning notes explain existing-entry-only execution, reported candidate ambiguity, explicit band vs threshold, direct artifact bindings and four dispatches for the example. Preserve source quotes in follow-ups and all prior baseline snapshots.

- [ ] **Step 4: Run GREEN.** `.venv/bin/python -m pytest tests/test_local_matlab_planning.py tests/test_agent_planner.py tests/test_agent_runtime.py tests/test_agent_web.py tests/test_agent_history.py tests/test_agent_recovery.py -q`。
- [ ] **Step 5: Commit.** `feat: plan local MATLAB analysis from discovered project entries`，包含 required trailers。

### Task 5: Three Inputs, Local Defaults and CLI Compatibility

**Files:** Modify `verification/routes.py`, `cli.py`, `web.py`, `static/agent.html`, `static/agent.js`, `static/agent.css` only if layout requires. Create `tests/test_matlab_project_web.py`, `tests/test_matlab_project_browser.py`; extend `tests/test_directory_cli.py`.

**Consumes:** Local resource registration and existing conversation API.

**Produces:** Optional `analysis_request` / `evaluation_criteria` request fields, `MessageRequest.canonical_message() -> str`, and CLI --location / --analysis-request / --criteria support.

- [ ] **Step 1: Write RED tests.** Check original text survives, each field length is bounded, message-only clients work, conflicting dual forms are rejected, and old scenario aliases remain unchanged. Add browser checks for the separate labels, default local directory, unavailable Engine message, no execution on browsing, consent and report downloads.

```python
def test_structured_requirements_keep_source_text():
    from flight_agent.verification.routes import MessageRequest
    request = MessageRequest(
        analysis_request="Stick 阶跃；非线性闭环时域仿真；stepinfo",
        evaluation_criteria="超调量 ≤10%；调节时间 ≤3 s（5%误差带）",
    )
    text = request.canonical_message()
    assert "Stick 阶跃；非线性闭环时域仿真；stepinfo" in text
    assert "超调量 ≤10%；调节时间 ≤3 s（5%误差带）" in text
```

- [ ] **Step 2: Run RED.** `.venv/bin/python -m pytest tests/test_matlab_project_web.py tests/test_directory_cli.py -q`；浏览器测试单独启用，不能以默认跳过代替验证。
- [ ] **Step 3: Implement compatible input handling.** Use an explicit message-or-structured-input model validator. Reject a nonempty old message combined with structured fields rather than trying to infer semantic equivalence. Keep combined text within the existing server message limit and preserve labels/original excerpts. `ConversationManager.submit` passes `canonical_message()` to the runtime.

UI keeps `#message` as the analysis request/conversation field so existing chat/history behaviors remain recognizable, adds `#evaluation-criteria`, and clears only fields belonging to an accepted request. Send structured fields only when criteria are present, otherwise use the compatible message form. New conversations default to matlab_project; restored conversations retain their saved resource, not a newly chosen default.

CLI propose accepts mutually exclusive --request and --analysis-request, optional --criteria, explicit --location. Do not reinterpret old --scenario d1 as a local directory. Add local backend availability to bootstrap without exposing executable paths or credentials.

- [ ] **Step 4: Run GREEN.** `.venv/bin/python -m pytest tests/test_matlab_project_web.py tests/test_directory_cli.py tests/test_agent_web.py tests/test_native_directories.py tests/test_directories.py -q`；再执行 `RUN_BROWSER_TESTS=1 .venv/bin/python -m pytest tests/test_matlab_project_browser.py tests/test_agent_browser.py -q`。
- [ ] **Step 5: Commit.** `feat: expose local project analysis and evaluation inputs`，包含 required trailers。

### Task 6: End-to-End Acceptance, Real MATLAB Checks and User Documentation

**Files:** Create `tests/test_matlab_project_acceptance.py`, `tests/test_local_matlab_integration.py`. Modify `README.md`, `.env.example`, `docs/d1-agent.md`, and the design status after implementation is verified.

**Consumes:** All five preceding tasks and existing DOCX/JSON delivery.

**Produces:** The requested complete example and a second unrelated prepared-project case, optional live checks, deployment/run instructions and an honest verification record.

- [ ] **Step 1: Write acceptance RED tests.** Drive three inputs through proposal, exact-version confirmation, existing entry/model execution, series extraction, stepinfo, sourced comparison and Word/JSON download. Verify 10% and 3 s boundaries without rounded-value comparisons. Include missing script, ambiguous model, no logs, non-steady/multi-step data, restart with an unknown original call and changed source before confirmation.

```python
def test_requested_case_delivers_real_dependency_evidence(completed_project_case):
    record, report_json = completed_project_case
    assert record.task.baseline.confirmed
    assert record.task.budget_state.calls == 4
    assert {check.unit for check in record.task.result.evaluation.checks} == {"%", "s"}
    assert report_json["evaluation_requested"] is True
    assert record.task.result.unmet_requirements == []
```

`completed_project_case` is defined in the acceptance module. Offline tests use controlled planner/Engine fixtures explicitly; live tests use actual Engine and generated synthetic projects and are marked matlab.

- [ ] **Step 2: Run RED.** `.venv/bin/python -m pytest tests/test_matlab_project_acceptance.py -q`；补齐接入缺陷，不添加演示成功分支。
- [ ] **Step 3: Finish the acceptance path and documentation.** Document three inputs, existing-entry-only boundary, optional discovery manifest, source-vs-output separation, local interpreter configuration, trusted-project flag, required MATLAB products and precise unavailable/error behavior. Preserve Worker instructions as an alternative, not a local prerequisite.

Document this command shape without claiming it ran:

```bash
MATLAB_ALLOW_TRUSTED_PROJECTS=1 \
MATLAB_PYTHON_EXECUTABLE=/absolute/path/to/compatible/python \
flight-agent web --port 8000
```

Live tests create two differently named temporary synthetic projects, including MATLAB-only input/output generation and a prepared Simulink model; initialization and model preparation belong to test fixtures, never production agent-generated scripts. Compare actual metrics to independently specified reference values/tolerances. An actual user project and live model-service acceptance remain unverified until explicitly supplied and run.

- [ ] **Step 4: Verify exact outcomes.**

```bash
RUN_BROWSER_TESTS=0 RUN_MATLAB_TESTS=0 .venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src
RUN_BROWSER_TESTS=1 .venv/bin/python -m pytest tests/test_matlab_project_browser.py tests/test_agent_browser.py -q
RUN_MATLAB_TESTS=1 .venv-matlab/bin/python -m pytest tests/test_local_matlab_integration.py -m matlab -v
git diff --check
```

Run the live command only if that compatible environment is actually available; otherwise report it as not verified, never as passed. Do not install dependencies preemptively. On failures investigate the cause; never disable checks or expand tool permissions.

- [ ] **Step 5: Review, commit and report.** Request a focused review of the completed changes, fix directly related issues, rerun affected checks, then commit as `test: verify generic local MATLAB project analysis` with required trailers. Report exact pass/skip counts, actual live/browser coverage and any external acceptance gap. Do not attempt another push unless requested.

## Plan Self-Review

- Spec sections 1–4: Tasks 1, 4 and 5 cover existing-entry boundary, three inputs, static discovery, ambiguity and local resource identity.
- Spec sections 5–6: Tasks 2–4 cover both execution modes, independent stepinfo, explicit artifacts, interpreter selection, lifecycle, consent and original-ID recovery.
- Spec section 7: Tasks 3, 5 and 6 cover the exact user example, parameter/threshold distinction, real evidence and report content.
- Spec sections 8–9: Every task has RED/GREEN checks; Task 6 covers varied projects, HTTP/browser/live validation, compatibility and the stated non-goals.
- Shared names, tool names, request fields, series descriptor and local resource host are defined once in File Map and used consistently.
- Existing state, source projects and old MATLAB helper defaults remain unchanged by new workflow defaults.
