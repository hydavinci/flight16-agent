# MATLAB Single-File Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore `/workbench` as a distinct Worker-only page that uploads one trusted `.m` file, executes the legacy `params`/`result` contract, and displays bounded JSON results, logs, MATLAB version, figures, and recent runs.

**Architecture:** Add a strict shared workbench contract and a dedicated Worker-side run store/executor/router under `/workbench`. The Web application exposes only an authenticated `/api/workbench` proxy and a separate native-browser page; it does not execute or persist uploaded MATLAB source. The existing Agent homepage, conversation runtime, project execution routes, and shared Worker execution slot remain unchanged.

**Tech Stack:** Python 3.11/3.12, FastAPI, Pydantic v2, httpx, MATLAB Engine for Python, Pillow, vanilla ES modules, Playwright, pytest.

**Spec:** `docs/superpowers/specs/2026-11-02-matlab-single-file-workbench-design.md`

## Global Constraints

- Accept one trusted UTF-8 `.m` file up to exactly 1,000,000 bytes.
- Script contract: define `params`, execute the script, require `result`.
- Function contract: call `result = functionName(params)`.
- Execute only through the configured MATLAB Worker; never fall back to the Web process or local Engine.
- Keep Worker execution opt-in through `MATLAB_ALLOW_TRUSTED_PROJECTS=1`.
- Reject arbitrary paths, expressions, startup options, additional files, and positional-argument configuration.
- Bound parameters to 100 KB, result to 4 MB, log to 1 MB, each PNG to 2 MB, and figures to 5.
- Preserve the shared Worker execution slot, idempotent run IDs, explicit failure states, artifact digest checks, same-origin protection, API token authorization, CSP, and no-store headers.
- Do not restore the deleted fixed Agent, rules, knowledge base, or assessment flow.
- Do not add a Node runtime, bundler, or frontend dependency.

---

### Task 1: Define strict workbench request and record contracts

**Files:**
- Create: `src/flight_agent/workbench_contracts.py`
- Create: `tests/test_workbench_contracts.py`

**Interfaces:**
- Consumes: `flight_agent.models.Contract`, `JsonValue`, and finite JSON validation.
- Produces:
  - `WorkbenchRunRequest`
  - `WorkbenchExecutionReceipt`
  - `WorkbenchArtifact`
  - `WorkbenchRunRecord`
  - `workbench_request_digest(request: WorkbenchRunRequest) -> str`
  - limits `MAX_WORKBENCH_*`

- [ ] **Step 1: Write failing contract tests**

Create `tests/test_workbench_contracts.py` with focused cases:

```python
import pytest
from pydantic import ValidationError

from flight_agent.workbench_contracts import (
    WorkbenchRunRequest,
    workbench_request_digest,
)


def request_data():
    return {
        "id": "a" * 32,
        "filename": "calculation.m",
        "source": "result = struct('value', params.value * 2);",
        "kind": "script",
        "parameters": {"value": 3},
        "trusted": True,
        "timeout_seconds": 120,
    }


def test_workbench_request_preserves_legacy_script_contract():
    request = WorkbenchRunRequest.model_validate(request_data())
    assert request.filename == "calculation.m"
    assert request.parameters == {"value": 3}
    assert len(workbench_request_digest(request)) == 64


def test_workbench_function_requires_matching_single_params_signature():
    data = request_data()
    data.update(
        kind="function",
        source="function result = calculation(params)\nresult = params;\nend",
    )
    assert WorkbenchRunRequest.model_validate(data).kind == "function"
    data["source"] = "function result = wrong(params)\nresult = params;\nend"
    with pytest.raises(ValidationError, match="function"):
        WorkbenchRunRequest.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("filename", "../calculation.m"),
        ("filename", "for.m"),
        ("filename", "flight_agent_execute_workbench.m"),
        ("source", ""),
        ("source", "\0"),
        ("source", "x" * 1_000_001),
        ("parameters", {"value": float("nan")}),
        ("parameters", {"value": "x" * 100_001}),
        ("trusted", False),
        ("trusted", "true"),
        ("timeout_seconds", 0),
        ("timeout_seconds", 601),
    ],
)
def test_workbench_request_rejects_untrusted_or_unbounded_input(field, value):
    data = request_data()
    data[field] = value
    with pytest.raises(ValidationError):
        WorkbenchRunRequest.model_validate(data)
```

Also test that `WorkbenchRunRecord` rejects:

- success without MATLAB version or a completed execution stage;
- duplicate artifact paths;
- non-PNG figure artifact names;
- failed records containing success output;
- logs over 1 MB;
- non-finite or over-4-MB results.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run pytest tests/test_workbench_contracts.py -q
```

Expected: collection fails because `flight_agent.workbench_contracts` does not exist.

- [ ] **Step 3: Implement the contract module**

Create constants and strict models:

```python
MAX_WORKBENCH_SOURCE_BYTES = 1_000_000
MAX_WORKBENCH_PARAMETERS_BYTES = 100_000
MAX_WORKBENCH_RESULT_BYTES = 4_000_000
MAX_WORKBENCH_LOG_BYTES = 1_000_000
MAX_WORKBENCH_FIGURE_BYTES = 2_000_000
MAX_WORKBENCH_FIGURES = 5


class WorkbenchRunRequest(Contract):
    id: RunId
    filename: str
    source: str
    kind: Literal["script", "function"]
    parameters: dict[str, JsonValue]
    trusted: Annotated[bool, Field(strict=True)]
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=600)] = 120
```

Use the removed upload contract only as behavioral reference. Keep these
invariants:

- filename matches `[A-Za-z][A-Za-z0-9_]{0,62}\.m`;
- reject MATLAB keywords and names beginning `flight_agent_`;
- source is nonempty UTF-8 text without NUL and within the byte limit;
- parameters are a finite JSON object within 100 KB;
- `trusted` must be literal `True`;
- function source contains a declaration whose name matches the validated file
  stem, for example `function result = calculation(params)`.

Define `WorkbenchExecutionReceipt` for the child-process receipt:

```python
class WorkbenchExecutionReceipt(Contract):
    request_sha256: Digest
    result: JsonValue
    matlab_version: Annotated[str, Field(min_length=1, max_length=200)]
    figures: Annotated[list[str], Field(max_length=MAX_WORKBENCH_FIGURES)] = []
```

Define `WorkbenchArtifact` with `path`, `sha256`, `size`, `media_type`, and
literal role `"figure"`. Define `WorkbenchRunRecord` with the state fields from
the spec and a model validator that permits result/version/artifacts only for
`completed`.

Implement a canonical digest:

```python
def workbench_request_digest(request: WorkbenchRunRequest) -> str:
    content = json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run contract tests and lint**

Run:

```bash
uv run pytest tests/test_workbench_contracts.py -q
uv run ruff check src/flight_agent/workbench_contracts.py tests/test_workbench_contracts.py
uv run mypy src/flight_agent/workbench_contracts.py
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit the contract**

```bash
git add src/flight_agent/workbench_contracts.py tests/test_workbench_contracts.py
git commit -m "feat: define MATLAB workbench contracts"
```

---

### Task 2: Persist Worker-side workbench runs

**Files:**
- Create: `src/flight_agent/verification/workbench_store.py`
- Create: `tests/test_workbench_store.py`

**Interfaces:**
- Consumes:
  - `WorkbenchRunRequest`
  - `WorkbenchRunRecord`
  - `workbench_request_digest`
- Produces:
  - `WorkbenchRunStore(root: Path)`
  - `create(request) -> WorkbenchRunRecord`
  - `get(run_id) -> WorkbenchRunRecord`
  - `request(run_id) -> WorkbenchRunRequest`
  - `update(run_id, **changes) -> WorkbenchRunRecord`
  - `recent(limit: int = 20) -> list[WorkbenchRunRecord]`
  - `directory(run_id) -> Path`

- [ ] **Step 1: Write failing persistence tests**

Create tests for idempotent storage behavior:

```python
def test_workbench_store_persists_request_status_and_recent_order(tmp_path):
    store = WorkbenchRunStore(tmp_path / "runs")
    first = store.create(workbench_request("a" * 32))
    second = store.create(workbench_request("b" * 32))
    assert store.request(first.id).filename == "calculation.m"
    assert [item.id for item in store.recent()] == [second.id, first.id]


def test_workbench_store_marks_incomplete_runs_interrupted_after_restart(tmp_path):
    root = tmp_path / "runs"
    store = WorkbenchRunStore(root)
    record = store.create(workbench_request("a" * 32))
    store.update(record.id, status="running", stage="executing")
    recovered = WorkbenchRunStore(root).get(record.id)
    assert recovered.status == "interrupted"
    assert "restart" in recovered.error.lower()


def test_workbench_store_hides_unknown_and_unsafe_paths(tmp_path):
    store = WorkbenchRunStore(tmp_path / "runs")
    for run_id in ("../secret", "a" * 32):
        with pytest.raises(KeyError):
            store.get(run_id)
```

Also cover:

- symlinked run directories/status/request files;
- update timestamp changes;
- `recent(limit)` bounds;
- source remains in `request.json`, not `status.json`;
- creating an existing ID raises `FileExistsError`.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run pytest tests/test_workbench_store.py -q
```

Expected: import failure because `workbench_store.py` does not exist.

- [ ] **Step 3: Implement the store**

Follow `verification/store.py` without generalizing its project-specific types.
Use `safe_directory(root)` before creating files and `write_json` for atomic
JSON writes. On initialization, convert `queued` and `running` records to:

```python
status="interrupted"
stage="interrupted"
error="Worker restarted; execution state is unknown. Explicit review required."
```

Create records with filename/kind and request digest but no source. Store the
complete request only in `request.json`.

- [ ] **Step 4: Run store tests and targeted existing store tests**

Run:

```bash
uv run pytest tests/test_workbench_store.py \
  tests/test_verification_worker.py::test_worker_restart_marks_pending_run_interrupted_not_replayed -q
uv run ruff check src/flight_agent/verification/workbench_store.py tests/test_workbench_store.py
uv run mypy src/flight_agent/verification/workbench_store.py
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit Worker persistence**

```bash
git add src/flight_agent/verification/workbench_store.py tests/test_workbench_store.py
git commit -m "feat: persist MATLAB workbench runs"
```

---

### Task 3: Execute trusted single-file MATLAB requests and validate figures

**Files:**
- Create: `src/flight_agent/verification/workbench_matlab.py`
- Create: `src/flight_agent/matlab_support/flight_agent_execute_workbench.m`
- Create: `tests/test_workbench_matlab.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `WorkbenchRunRequest`, `WorkbenchExecutionReceipt`,
  `WorkbenchArtifact`, `_stop_worker`, `safe_directory`.
- Produces:
  - `execute_workbench(directory: Path, request: WorkbenchRunRequest) -> tuple[WorkbenchExecutionReceipt, list[WorkbenchArtifact], str]`
  - `validate_workbench_png(path: Path) -> None`

- [ ] **Step 1: Write failing executor tests**

Create controlled-engine tests for both contracts:

```python
@pytest.mark.parametrize("kind", ["script", "function"])
def test_workbench_executes_script_and_function_with_result_and_figure(
    tmp_path, fake_engine, kind
):
    request = workbench_request(kind=kind)
    receipt, artifacts, log = execute_workbench(tmp_path / "run", request)
    assert receipt.result == {"value": 6}
    assert receipt.matlab_version == "test-engine"
    assert [item.path for item in artifacts] == ["figure-1.png"]
    assert (tmp_path / "run/figure-1.png").read_bytes().startswith(b"\x89PNG")
    assert "workbench log" in log
```

Add parameterized explicit failures:

- MATLAB startup/license error;
- MATLAB execution error preserving the original message;
- missing receipt;
- request digest mismatch;
- missing `result`;
- NaN/Inf JSON;
- result over 4 MB;
- log over 1 MB;
- more than 5 figures;
- invalid PNG, image dimensions over 16 million pixels, PNG over 2 MB;
- symlinked receipt/artifact;
- timeout;
- process cleanup uncertainty.

- [ ] **Step 2: Run executor tests and verify RED**

Run:

```bash
uv run pytest tests/test_workbench_matlab.py -q
```

Expected: import failure because the executor does not exist.

- [ ] **Step 3: Implement the MATLAB wrapper**

Create `flight_agent_execute_workbench.m` from the previously proven wrapper,
with a request digest argument and receipt identity:

```matlab
function flight_agent_execute_workbench(run_dir, filename, kind, parameters_json, request_sha256, max_figures)
set(groot, 'defaultFigureVisible', 'off');
existing = findall(groot, 'Type', 'figure');
params = jsondecode(parameters_json);
source_path = fullfile(run_dir, 'code', filename);
result = execute_source(source_path, kind, params);

created = setdiff(findall(groot, 'Type', 'figure'), existing);
assert(numel(created) <= max_figures, 'Figure count exceeds the limit');
output.request_sha256 = request_sha256;
output.result = result;
output.matlab_version = version;
output.figures = cell(1, numel(created));
for i = 1:numel(created)
    output.figures{i} = sprintf('figure-%d.png', i);
    exportgraphics(created(i), fullfile(run_dir, output.figures{i}), 'Resolution', 120);
end
encoded = jsonencode(output, 'ConvertInfAndNaN', false);
file = fopen(fullfile(run_dir, 'receipt.json'), 'w', 'n', 'UTF-8');
assert(file ~= -1, 'Cannot write execution receipt');
cleanup = onCleanup(@() fclose(file));
fprintf(file, '%s', encoded);
close(created);
end

function result = execute_source(source_path, kind, params)
[source_dir, entry] = fileparts(source_path);
cd(source_dir);
if strcmp(kind, 'function')
    result = feval(entry, params);
else
    run(source_path);
    assert(exist('result', 'var') == 1, 'Script must assign a result variable');
end
end
```

- [ ] **Step 4: Implement the parent/child executor**

Use the existing `local_matlab.py` process pattern:

- write `code/{request.filename}` and `request.json`;
- sanitize LLM/OpenAI/LangSmith/Worker environment variables;
- spawn `python -u -m flight_agent.verification.workbench_matlab {directory}`;
- enforce timeout, log size, receipt size, process-group cleanup, and no silent
  success after cleanup uncertainty;
- child imports `matlab.engine`, starts MATLAB, adds only
  `flight_agent/matlab_support`, and calls the fixed wrapper;
- parent validates the receipt request digest;
- validate PNGs using Pillow with decompression-bomb warnings promoted to errors;
- describe each PNG with SHA-256, byte size, media type `image/png`, role
  `figure`;
- return bounded sanitized log separately.

Do not call the uploaded filename through `eval`; pass the validated stem to
the fixed MATLAB wrapper.

- [ ] **Step 5: Extend the fake Engine seam**

Update `tests/conftest.py` so the fake MATLAB Engine recognizes
`flight_agent_execute_workbench` and writes controlled receipts/PNGs for the
test modes. Keep existing project execution behavior unchanged.

- [ ] **Step 6: Run executor and regression tests**

Run:

```bash
uv run pytest tests/test_workbench_matlab.py tests/test_local_matlab.py -q
uv run ruff check src/flight_agent/verification/workbench_matlab.py \
  tests/test_workbench_matlab.py tests/conftest.py
uv run mypy src/flight_agent/verification/workbench_matlab.py
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit the executor**

```bash
git add src/flight_agent/verification/workbench_matlab.py \
  src/flight_agent/matlab_support/flight_agent_execute_workbench.m \
  tests/test_workbench_matlab.py tests/conftest.py
git commit -m "feat: execute trusted MATLAB workbench files"
```

---

### Task 4: Add authenticated Worker workbench routes

**Files:**
- Create: `src/flight_agent/verification/workbench_worker.py`
- Create: `tests/test_workbench_worker.py`
- Modify: `src/flight_agent/worker.py`
- Modify: `tests/test_verification_worker.py`

**Interfaces:**
- Consumes:
  - `WorkbenchRunStore`
  - `execute_workbench`
  - shared `BackgroundRunner`
- Produces:
  - `workbench_routes(root: Path, runner: BackgroundRunner) -> APIRouter`
  - Worker endpoints:
    - `GET /workbench/runs`
    - `POST /workbench/runs`
    - `GET /workbench/runs/{run_id}`
    - `GET /workbench/runs/{run_id}/artifacts/{name:path}`

- [ ] **Step 1: Write failing Worker route tests**

Create tests that build the real Worker app and assert:

```python
def test_workbench_worker_requires_auth_and_trusted_request(tmp_path, monkeypatch):
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    with TestClient(create_worker_app(tmp_path, TOKEN)) as client:
        request = workbench_request().model_dump(mode="json")
        assert client.post("/workbench/runs", json=request).status_code == 401
        assert client.post(
            "/workbench/runs",
            json={**request, "trusted": False},
            headers=HEADERS,
        ).status_code == 422


def test_workbench_worker_executes_idempotently_and_serves_verified_figure(
    tmp_path, fake_engine, monkeypatch
):
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    with TestClient(create_worker_app(tmp_path, TOKEN)) as client:
        request = workbench_request().model_dump(mode="json")
        first = client.post("/workbench/runs", json=request, headers=HEADERS)
        assert first.status_code == 202
        record = wait_job(client, request["id"], prefix="/workbench/runs")
        assert record["status"] == "completed"
        assert record["result"] == {"value": 6}
        repeated = client.post("/workbench/runs", json=request, headers=HEADERS)
        assert repeated.status_code == 202
        changed = {**request, "timeout_seconds": 20}
        assert client.post(
            "/workbench/runs", json=changed, headers=HEADERS
        ).status_code == 409
        image = client.get(
            f"/workbench/runs/{request['id']}/artifacts/figure-1.png",
            headers=HEADERS,
        )
        assert image.status_code == 200
        assert image.content.startswith(b"\x89PNG")
```

Also cover:

- recent runs return newest first and at most 20;
- request/source/receipt files are never downloadable;
- artifact corruption returns 409;
- the shared runner slot rejects a second different project/workbench request
  while either type is running;
- scheduling failure writes terminal failed state and releases the slot;
- Worker restart marks pending workbench runs interrupted;
- health reports `"single_file_execution": True`.

- [ ] **Step 2: Run Worker tests and verify RED**

Run:

```bash
uv run pytest tests/test_workbench_worker.py -q
```

Expected: 404 responses because `/workbench` Worker routes are absent.

- [ ] **Step 3: Implement `workbench_routes`**

Follow `verification/worker.py`:

- prefix router with `/workbench`;
- create `WorkbenchRunStore(root / "runs" / "workbench-worker")`;
- return an existing same-digest record before acquiring the runner slot;
- reject same ID/different digest with 409;
- acquire the shared `runner.slot` nonblocking;
- create the record before scheduling;
- update `running/executing`, call `execute_workbench`, then update
  `completed/completed` with result, MATLAB version, log, and artifacts;
- on background exceptions persist `failed/failed` and bounded error text;
- release the slot in `finally`;
- verify artifacts with the existing digest/containment helpers before
  `FileResponse`.

- [ ] **Step 4: Register routes in the Worker app**

Modify `create_worker_app`:

```python
app.include_router(project_routes(root, runner))
app.include_router(workbench_routes(root, runner))
```

Extend health without changing existing fields:

```python
"single_file_execution": True
```

Replace old tests that assert `/jobs` stays removed; `/jobs` must remain 404
while `/workbench/runs` is now supported.

- [ ] **Step 5: Run Worker and existing verification tests**

Run:

```bash
uv run pytest tests/test_workbench_worker.py tests/test_verification_worker.py -q
uv run ruff check src/flight_agent/verification/workbench_worker.py \
  src/flight_agent/worker.py tests/test_workbench_worker.py \
  tests/test_verification_worker.py
uv run mypy src/flight_agent/verification/workbench_worker.py src/flight_agent/worker.py
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit Worker routes**

```bash
git add src/flight_agent/verification/workbench_worker.py \
  src/flight_agent/worker.py tests/test_workbench_worker.py \
  tests/test_verification_worker.py
git commit -m "feat: expose MATLAB workbench worker runs"
```

---

### Task 5: Add RemoteClient methods and Web proxy routes

**Files:**
- Create: `src/flight_agent/web_routes/workbench.py`
- Create: `tests/test_workbench_web.py`
- Modify: `src/flight_agent/remote.py`
- Modify: `src/flight_agent/web.py`
- Modify: `src/flight_agent/web_routes/pages.py`
- Modify: `tests/test_web_modules.py`
- Modify: `tests/test_web_surface.py`

**Interfaces:**
- Consumes:
  - Worker `/workbench` endpoints
  - existing Web API token dependency
  - existing same-origin/host/body/CSP middleware
- Produces RemoteClient methods:
  - `submit_workbench(request) -> WorkbenchRunRecord`
  - `recent_workbench() -> list[WorkbenchRunRecord]`
  - `workbench_status(run_id) -> WorkbenchRunRecord`
  - `workbench_artifact(run_id, path) -> bytes`
- Produces Web endpoints:
  - `GET /api/workbench/bootstrap`
  - `GET /api/workbench/examples/{name}`
  - `GET /api/workbench/runs`
  - `POST /api/workbench/runs`
  - `GET /api/workbench/runs/{run_id}`
  - `GET /api/workbench/runs/{run_id}/artifacts/{name:path}`

- [ ] **Step 1: Write failing RemoteClient tests**

In `tests/test_workbench_web.py`, use `httpx.MockTransport` to assert:

- authorization header is present;
- POST uses `/workbench/runs`;
- 409 remains distinguishable from Worker-unavailable errors;
- response size is bounded;
- no automatic resubmission occurs after a transport or authorization error;
- artifact bytes are bounded to 2 MB.

Define a status-bearing error in `remote.py`:

```python
class RemoteHTTPError(RemoteError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
```

Existing remote project behavior must retain its current messages and retry
semantics.

- [ ] **Step 2: Write failing Web proxy tests**

Test a Web app connected to the real controlled Worker:

```python
def test_web_workbench_proxies_worker_run_without_local_execution(
    tmp_path, fake_engine, monkeypatch
):
    with configured_worker(tmp_path, monkeypatch):
        with TestClient(create_web_app(tmp_path), base_url="http://127.0.0.1") as client:
            token = client.get("/api/workbench/bootstrap").json()["token"]
            headers = {"x-flight-token": token}
            response = client.post(
                "/api/workbench/runs",
                json=workbench_request().model_dump(mode="json"),
                headers=headers,
            )
            assert response.status_code == 202
            record = wait_job(
                client, response.json()["id"], headers, "/api/workbench/runs"
            )
            assert record["result"] == {"value": 6}
            assert not (tmp_path / "runs/workbench-web").exists()
```

Also assert:

- `/workbench` content differs from `/`;
- missing token/cross-origin/bad Host are rejected;
- bootstrap exposes configured/error state but never Worker token;
- unconfigured Worker returns 503 without fake result;
- Worker busy maps to 409;
- unknown run/artifact maps to 404;
- `/api/jobs` remains 404;
- only `web_script.m` and `web_function.m` examples are allowed.

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
uv run pytest tests/test_workbench_web.py tests/test_web_surface.py -q
```

Expected: 404 responses for `/api/workbench` and identical `/workbench` content.

- [ ] **Step 4: Implement RemoteClient workbench methods**

Reuse `_request`; do not add another HTTP client class. Parse Worker JSON into
strict workbench models. Preserve status codes through `RemoteHTTPError`.

Do not add polling to `submit_workbench`; the browser/Web status endpoint owns
polling. This keeps POST bounded and avoids tying up a Web request for MATLAB
duration.

- [ ] **Step 5: Implement the Web workbench router**

Define:

```python
def workbench_router(
    token: str,
    authorize: Callable[..., None],
    examples: Path,
    remote_factory: Callable[[], RemoteClient] = RemoteClient.from_environment,
) -> APIRouter:
    ...
```

Rules:

- bootstrap is unauthed, returns token and Worker configured/error status;
- all other endpoints depend on `authorize`;
- instantiate the RemoteClient per request and close owned HTTP clients;
- map Worker 409/404 to the same status;
- map configuration, transport, 401/403, and unexpected upstream failures to
  explicit 503 text without leaking tokens;
- stream only verified bounded artifact bytes with `image/png`.

Register it in `create_web_app` after `pages_router`.

- [ ] **Step 6: Separate the page route and static allowlist**

Change `/workbench` to:

```python
return FileResponse(static / "matlab-workbench.html", media_type="text/html")
```

Add `matlab-workbench.js` and `matlab-workbench.css` to `STATIC_ASSETS`; do not
allow HTML through `/static/{name}`.

Update route ownership expectations in `tests/test_web_modules.py`.

- [ ] **Step 7: Run Web route and regression tests**

Run:

```bash
uv run pytest tests/test_workbench_web.py tests/test_web_surface.py \
  tests/test_web_modules.py tests/test_agent_web.py -q
uv run ruff check src/flight_agent/remote.py src/flight_agent/web.py \
  src/flight_agent/web_routes/pages.py src/flight_agent/web_routes/workbench.py \
  tests/test_workbench_web.py tests/test_web_surface.py tests/test_web_modules.py
uv run mypy src/flight_agent/remote.py src/flight_agent/web_routes/workbench.py
```

Expected: all commands exit 0.

- [ ] **Step 8: Commit Web/Worker transport**

```bash
git add src/flight_agent/remote.py src/flight_agent/web.py \
  src/flight_agent/web_routes/pages.py src/flight_agent/web_routes/workbench.py \
  tests/test_workbench_web.py tests/test_web_surface.py tests/test_web_modules.py
git commit -m "feat: proxy MATLAB workbench runs"
```

---

### Task 6: Build the dedicated native-browser workbench

**Files:**
- Create: `src/flight_agent/static/matlab-workbench.html`
- Create: `src/flight_agent/static/matlab-workbench.css`
- Create: `src/flight_agent/static/matlab-workbench.js`
- Create: `tests/test_workbench_browser.py`
- Modify: `tests/test_frontend_modules.py`
- Modify: `tests/test_agent_layout_browser.py`

**Interfaces:**
- Consumes: `/api/workbench/bootstrap`, examples, runs, status, artifacts.
- Produces: the complete single-file workbench user experience.

- [ ] **Step 1: Write failing static-page contract tests**

Assert `/workbench` HTML has:

- title `MATLAB 分析工作台`;
- one `.m` file input;
- script/function select;
- timeout input;
- JSON parameters textarea;
- trusted-code checkbox;
- run button;
- result, figures, log, and recent-runs containers;
- return link to `/`;
- no conversation composer, Agent analysis panel, or Node-built asset.

Assert the script is loaded with `defer` or `type="module"` and the two new
assets are in the explicit allowlist.

- [ ] **Step 2: Write failing Playwright tests**

Use a controlled Web/Worker pair and test both 1440 and 390 widths:

```python
@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390])
def test_workbench_runs_script_and_renders_result_figure_log_and_history(
    tmp_path, fake_engine, monkeypatch, width
):
    with workbench_server(tmp_path, monkeypatch) as url:
        page.goto(url + "/workbench")
        expect(page).to_have_title("MATLAB 分析工作台")
        page.set_input_files(
            "#file",
            {
                "name": "calculation.m",
                "mimeType": "text/plain",
                "buffer": b"result = struct('value', params.value * 2); plot(1, 1);",
            },
        )
        page.locator("#parameters").fill('{"value": 3}')
        page.locator("#trusted").check()
        page.locator("#run-button").click()
        expect(page.locator("#execution-status")).to_have_text("执行完成")
        expect(page.locator("#result-data")).to_contain_text('"value": 6')
        expect(page.locator("#figures img")).to_have_count(1)
        expect(page.locator("#execution-log")).to_contain_text("workbench log")
        expect(page.locator("#history")).to_contain_text("calculation.m")
```

Add cases for:

- function example;
- unconfigured Worker;
- invalid JSON;
- untrusted submission;
- failed Worker run with no success-shaped result;
- reloading and selecting recent history;
- result JSON download;
- figure object URL cleanup when switching runs;
- no horizontal overflow.

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
RUN_BROWSER_TESTS=1 uv run pytest tests/test_workbench_browser.py \
  tests/test_frontend_modules.py -q
```

Expected: missing page/assets and failed UI assertions.

- [ ] **Step 4: Implement the HTML and CSS**

Use the approved layout:

- header with Worker state and return-to-Agent link;
- left input panel;
- right result/status/figures/log panel;
- recent-runs panel;
- responsive breakpoint consistent with current Agent CSS;
- no inline script/style, preserving CSP.

Reuse visual tokens from `agent.css` by copying only the necessary values into
the dedicated stylesheet; do not make either page depend on the other's DOM.

- [ ] **Step 5: Implement the browser client**

The JS must:

- fetch bootstrap before enabling submission;
- retain user input during bootstrap;
- decode the selected file as fatal UTF-8;
- validate `.m`, 1 MB, JSON object, finite numbers, timeout, and trust;
- generate a 32-hex run ID with `crypto.getRandomValues`;
- POST once and never automatically resubmit;
- poll every 800 ms only while queued/running;
- stop polling on terminal status or selection change;
- render strict record fields;
- fetch each manifest figure through its artifact endpoint;
- revoke stale object URLs;
- download result through a local Blob;
- list/select recent runs;
- display explicit errors in the notice region.

- [ ] **Step 6: Update existing frontend/layout tests**

Change `tests/test_agent_layout_browser.py` so:

- `/` title remains `Flight Agent`;
- `/workbench` title is `MATLAB 分析工作台`;
- navigating to `/workbench` no longer expects conversation restoration.

Keep all homepage module tests unchanged except the static allowlist additions.

- [ ] **Step 7: Run targeted and full browser tests**

Run:

```bash
RUN_BROWSER_TESTS=1 RUN_MATLAB_TESTS=0 uv run pytest \
  tests/test_workbench_browser.py tests/test_frontend_modules.py \
  tests/test_agent_layout_browser.py -q
RUN_BROWSER_TESTS=1 RUN_MATLAB_TESTS=0 uv run pytest \
  -m "browser and not matlab" -q
```

Expected: both commands exit 0.

- [ ] **Step 8: Commit the workbench UI**

```bash
git add src/flight_agent/static/matlab-workbench.html \
  src/flight_agent/static/matlab-workbench.css \
  src/flight_agent/static/matlab-workbench.js \
  tests/test_workbench_browser.py tests/test_frontend_modules.py \
  tests/test_agent_layout_browser.py
git commit -m "feat: restore MATLAB analysis workbench"
```

---

### Task 7: Restore examples, document startup, and add licensed acceptance

**Files:**
- Create: `examples/web_script.m`
- Create: `examples/web_function.m`
- Modify: `README.md`
- Modify: `docs/usage.md`
- Modify: `docs/architecture.md`
- Modify: `tests/test_workbench_matlab.py`
- Modify: `tests/test_ci_contract.py`
- Modify: `.github/workflows/ci.yml` only if the existing MATLAB selector does not collect the new acceptance tests.

**Interfaces:**
- Consumes: completed workbench flow.
- Produces: runnable examples, documented startup, and release evidence.

- [ ] **Step 1: Add example-content tests**

Assert:

- `web_script.m` reads `params`, assigns `result`, and creates a figure;
- `web_function.m` declares exactly
  `function result = web_function(params)` and creates a figure;
- the Web example route serves only these two exact names.

- [ ] **Step 2: Add licensed MATLAB acceptance tests**

Under `@pytest.mark.matlab`, add:

```python
@pytest.mark.matlab
@pytest.mark.skipif(
    os.environ.get("RUN_MATLAB_TESTS") != "1",
    reason="Live MATLAB is opt-in",
)
@pytest.mark.parametrize("kind", ["script", "function"])
def test_live_workbench_executes_result_and_figure(tmp_path, kind):
    request = live_workbench_request(kind)
    receipt, artifacts, log = execute_workbench(tmp_path / kind, request)
    assert receipt.result == {"value": 6}
    assert receipt.matlab_version
    assert [item.path for item in artifacts] == ["figure-1.png"]
    assert (tmp_path / kind / "figure-1.png").read_bytes().startswith(b"\x89PNG")
```

Also preserve the original MATLAB error text in a live failing script test.

- [ ] **Step 3: Run tests and verify RED before examples/docs**

Run:

```bash
uv run pytest tests/test_workbench_matlab.py tests/test_workbench_web.py \
  tests/test_ci_contract.py -q
```

Expected: example assertions fail because files are absent; licensed tests skip
unless explicitly enabled.

- [ ] **Step 4: Add the two examples**

Script:

```matlab
frequency_hz = params.frequency_hz;
amplitude = params.amplitude;
samples = params.samples;
t = linspace(0, 2, samples);
y = amplitude * sin(2 * pi * frequency_hz * t);
plot(t, y);
xlabel('Time (s)');
ylabel('Amplitude');
grid on;
result = struct('samples', samples, 'peak', max(y), 'minimum', min(y));
```

Function:

```matlab
function result = web_function(params)
t = linspace(0, 2, params.samples);
y = params.amplitude * cos(2 * pi * params.frequency_hz * t);
plot(t, y);
xlabel('Time (s)');
ylabel('Amplitude');
grid on;
result = struct('samples', params.samples, 'peak', max(y), 'minimum', min(y));
end
```

- [ ] **Step 5: Update documentation**

Document exact startup:

```bash
MATLAB_ALLOW_TRUSTED_PROJECTS=1 \
MATLAB_WORKER_TOKEN='0123456789abcdef0123456789abcdef' \
.venv-matlab/bin/flight-agent worker --port 9001

MATLAB_WORKER_URL='http://127.0.0.1:9001' \
MATLAB_WORKER_TOKEN='0123456789abcdef0123456789abcdef' \
.venv/bin/flight-agent web --native-directory-picker --port 8000
```

State:

- `/` is the Agent homepage;
- `/workbench` is the Worker-only single-file MATLAB page;
- uploaded code has Worker account permissions and is not sandboxed;
- no local fallback exists;
- runs remain on the Worker filesystem until administrator cleanup.

Update architecture docs with the new routers/store/executor and shared Worker
slot.

- [ ] **Step 6: Verify the CI selector**

Run:

```bash
uv run pytest tests/test_ci_contract.py -q
uv run pytest --collect-only -m "matlab" -q | grep live_workbench
```

If the existing manual MATLAB job already runs all `matlab` tests, do not edit
the workflow. Edit `.github/workflows/ci.yml` only if collection proves the
new tests are excluded.

- [ ] **Step 7: Run the complete software and browser gates**

Use a real non-symlink temporary directory on macOS:

```bash
TMPDIR=/private/tmp RUN_BROWSER_TESTS=0 RUN_MATLAB_TESTS=0 \
  MATLAB_ALLOW_TRUSTED_PROJECTS=0 \
  uv run python -m pytest -m "not matlab and not browser" -q

RUN_BROWSER_TESTS=1 RUN_MATLAB_TESTS=0 \
  uv run python -m pytest -m "browser and not matlab" -q

uv run ruff check .
uv run mypy src
uv run python -m build --no-isolation
uv lock --check
git diff --exit-code -- uv.lock
git diff --check
```

Expected: every command exits 0. If the known macOS process-group permission
test fails only in the full local suite, rerun that exact test separately and
require the Linux CI software jobs to pass before completion; do not weaken the
cleanup behavior or test.

- [ ] **Step 8: Run licensed acceptance when the trusted runner is available**

Run:

```bash
RUN_MATLAB_TESTS=1 MATLAB_ALLOW_TRUSTED_PROJECTS=1 \
  uv run pytest tests/test_workbench_matlab.py -m matlab -q
```

Expected: script, function, figure, and original-error tests pass with a real
MATLAB Engine. If no licensed runner is available, record this as the only
unexecuted acceptance gate; do not claim live MATLAB verification.

- [ ] **Step 9: Commit examples and documentation**

```bash
git add examples/web_script.m examples/web_function.m README.md docs/usage.md \
  docs/architecture.md tests/test_workbench_matlab.py tests/test_ci_contract.py \
  .github/workflows/ci.yml
git commit -m "docs: describe MATLAB workbench execution"
```

---

### Task 8: Review the complete branch and publish

**Files:**
- Review all changes since `092d27e`.

**Interfaces:**
- Consumes: Tasks 1-7.
- Produces: reviewed, pushed branch with green required CI.

- [ ] **Step 1: Request a code review**

Review against:

- the approved spec;
- strict Worker-only execution;
- no source exposure;
- no fallback;
- idempotency and uncertainty handling;
- artifact containment/digests;
- homepage and project-runtime non-regression.

Fix every Critical and Important finding through a new RED/GREEN cycle.

- [ ] **Step 2: Re-run affected targeted tests after review fixes**

Run the smallest tests covering each fix, then rerun:

```bash
uv run pytest tests/test_workbench_contracts.py tests/test_workbench_store.py \
  tests/test_workbench_matlab.py tests/test_workbench_worker.py \
  tests/test_workbench_web.py -q
RUN_BROWSER_TESTS=1 uv run pytest tests/test_workbench_browser.py -q
```

Expected: all commands exit 0.

- [ ] **Step 3: Re-run complete release gates**

Repeat every command from Task 7 Step 7 after the final review fix.

- [ ] **Step 4: Push and verify GitHub Actions**

```bash
git push origin optimize/stages-2-6
gh run list --branch optimize/stages-2-6 --limit 3
RUN_ID=$(gh run list --branch optimize/stages-2-6 --limit 1 \
  --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN_ID" --exit-status
```

Required jobs:

- Software Python 3.11: success;
- Software Python 3.12: success;
- Browser Chromium: success;
- Licensed MATLAB acceptance: skipped unless manually requested on the trusted
  default-branch runner.

- [ ] **Step 5: Verify final repository state**

```bash
git status --short --branch
git rev-parse HEAD
git rev-parse origin/optimize/stages-2-6
```

Expected: clean worktree and identical local/remote SHAs.
