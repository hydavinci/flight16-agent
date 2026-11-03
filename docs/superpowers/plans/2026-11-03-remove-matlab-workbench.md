# Remove MATLAB Single-File Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Completely remove the dedicated MATLAB single-file workbench while preserving the Agent homepage and project-oriented MATLAB execution.

**Architecture:** Delete the workbench from the outside inward: first remove the Web/UI and remote proxy surface, then remove the Worker persistence/execution subsystem, then remove packaged resources and documentation. Preserve shared MATLAB process cleanup and the generic licensed MATLAB CI selector.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, native HTML/CSS/JavaScript, pytest, Playwright, Ruff, mypy, setuptools, GitHub Actions

**Spec:** `docs/superpowers/specs/2026-11-03-remove-matlab-workbench-design.md`

## Global Constraints

- `/workbench`, `/api/workbench/*`, and Worker `/workbench/*` must return 404.
- Remove every visible workbench link, page, API, execution path, example, and packaged resource.
- Preserve `/` as the Agent homepage.
- Preserve project verification routes and project-oriented MATLAB execution.
- Preserve shared Windows MATLAB process-tree cleanup in `src/flight_agent/matlab.py`.
- Preserve the generic `pytest -m matlab` CI selector.
- Do not redirect `/workbench` to `/`.
- Do not automatically delete historical `runs/workbench-worker/` administrator data.

---

### Task 1: Remove the Web, browser, and remote workbench surface

**Files:**
- Delete: `src/flight_agent/web_routes/workbench.py`
- Delete: `src/flight_agent/static/matlab-workbench.html`
- Delete: `src/flight_agent/static/matlab-workbench.css`
- Delete: `src/flight_agent/static/matlab-workbench.js`
- Delete: `tests/test_workbench_web.py`
- Delete: `tests/test_workbench_browser.py`
- Modify: `src/flight_agent/web.py`
- Modify: `src/flight_agent/remote.py`
- Modify: `src/flight_agent/web_routes/pages.py`
- Modify: `tests/test_web_surface.py`
- Modify: `tests/test_web_modules.py`
- Modify: `tests/test_frontend_modules.py`
- Modify: `tests/test_agent_layout_browser.py`

**Interfaces:**
- Consumes: the existing Agent homepage routes and project `RemoteClient._request`.
- Produces: no workbench Web route, API, static asset, browser client, or remote method.

- [ ] **Step 1: Replace positive Web tests with failing absence contracts**

Update `tests/test_web_surface.py` so the first test keeps the homepage checks
and proves all former workbench routes are absent:

```python
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/workbench"),
        ("GET", "/api/workbench/bootstrap"),
        ("GET", "/api/workbench/examples/web_script.m"),
        ("GET", "/api/workbench/runs"),
        ("POST", "/api/workbench/runs"),
        ("GET", "/api/workbench/runs/" + "a" * 32),
        (
            "GET",
            "/api/workbench/runs/"
            + "a" * 32
            + "/artifacts/figure-1.png",
        ),
    ],
)
def test_web_rejects_removed_workbench_surface(tmp_path, method, path):
    app, _, _, _ = make_agent_web(tmp_path, [])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.request(method, path, json={})
        assert response.status_code == 404


def test_web_exposes_only_agent_page_and_assets(tmp_path):
    app, _, _, _ = make_agent_web(tmp_path, [])
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert "<title>Flight Agent</title>" in client.get("/").text
        for name in ("agent.js", "agent.css", "favicon.svg"):
            assert client.get(f"/static/{name}").status_code == 200
        for name in (
            "matlab-workbench.js",
            "matlab-workbench.css",
            "matlab-workbench.html",
        ):
            assert name not in STATIC_ASSETS
            assert client.get(f"/static/{name}").status_code == 404
```

Remove the now-obsolete test that obtains a token from
`/api/workbench/bootstrap`.

In `tests/test_web_modules.py`, delete every `EXPECTED` entry whose path begins
with `/workbench` or `/api/workbench`.

In `tests/test_frontend_modules.py`, remove `WORKBENCH_ASSETS` and
`test_workbench_page_has_dedicated_csp_safe_controls`; keep all Agent module
and no-Node assertions unchanged.

In `tests/test_agent_layout_browser.py`, remove workbench navigation/title
expectations and retain all homepage layout/conversation assertions.

- [ ] **Step 2: Run the absence tests and verify RED**

Run:

```bash
TMPDIR=/private/tmp RUN_BROWSER_TESTS=0 uv run pytest \
  tests/test_web_surface.py tests/test_web_modules.py \
  tests/test_frontend_modules.py -q
```

Expected: failures show `/workbench` and `/api/workbench/*` still return
successful responses and workbench assets remain allowlisted.

- [ ] **Step 3: Remove Web route registration and static assets**

In `src/flight_agent/web.py`:

- remove `from .web_routes.workbench import workbench_router`;
- remove `app.include_router(workbench_router(token, authorize))`.

In `src/flight_agent/web_routes/pages.py`:

- remove `matlab-workbench.js` and `matlab-workbench.css` from `STATIC_ASSETS`;
- delete the `/workbench` route completely.

Delete the workbench router and all three workbench static files. Do not add a
redirect or replacement handler.

- [ ] **Step 4: Remove workbench-only RemoteClient code**

In `src/flight_agent/remote.py`:

- remove `quote`, `TypeAdapter`, and all `workbench_contracts` imports;
- remove `RemoteHTTPError`;
- change the non-success branch in `_request` back to:

```python
raise RemoteError(
    f"Worker returned HTTP {response.status_code}; submission not retried"
)
```

- delete `_workbench_request`, `submit_workbench`, `recent_workbench`,
  `workbench_status`, and `workbench_artifact`.

Keep `RemoteClient` URL/token validation and `_request` behavior used by
project execution unchanged.

- [ ] **Step 5: Delete dedicated Web/browser tests and run regressions**

Delete `tests/test_workbench_web.py` and `tests/test_workbench_browser.py`.

Run:

```bash
TMPDIR=/private/tmp RUN_BROWSER_TESTS=0 uv run pytest \
  tests/test_web_surface.py tests/test_web_modules.py \
  tests/test_frontend_modules.py tests/test_agent_web.py \
  tests/test_verification_worker.py -q

TMPDIR=/private/tmp RUN_BROWSER_TESTS=1 RUN_MATLAB_TESTS=0 uv run pytest \
  tests/test_agent_layout_browser.py -q

uv run ruff check src/flight_agent/web.py src/flight_agent/remote.py \
  src/flight_agent/web_routes/pages.py tests/test_web_surface.py \
  tests/test_web_modules.py tests/test_frontend_modules.py \
  tests/test_agent_layout_browser.py
uv run mypy src/flight_agent/web.py src/flight_agent/remote.py
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit the Web removal**

```bash
git add -A src/flight_agent/web.py src/flight_agent/remote.py \
  src/flight_agent/web_routes src/flight_agent/static \
  tests/test_web_surface.py tests/test_web_modules.py \
  tests/test_frontend_modules.py tests/test_agent_layout_browser.py \
  tests/test_workbench_web.py tests/test_workbench_browser.py
git commit -m "refactor: remove MATLAB workbench web surface"
```

---

### Task 2: Remove the Worker single-file execution subsystem

**Files:**
- Delete: `src/flight_agent/workbench_contracts.py`
- Delete: `src/flight_agent/verification/workbench_store.py`
- Delete: `src/flight_agent/verification/workbench_matlab.py`
- Delete: `src/flight_agent/verification/workbench_worker.py`
- Delete: `src/flight_agent/matlab_support/flight_agent_execute_workbench.m`
- Delete: `tests/test_workbench_contracts.py`
- Delete: `tests/test_workbench_store.py`
- Delete: `tests/test_workbench_matlab.py`
- Delete: `tests/test_workbench_worker.py`
- Modify: `src/flight_agent/worker.py`
- Modify: `tests/test_verification_worker.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: existing project `project_routes(root, runner)`.
- Produces: a Worker with project execution only and no single-file endpoint.

- [ ] **Step 1: Add failing Worker absence and health tests**

Replace `test_worker_health_reports_project_and_single_file_execution` in
`tests/test_verification_worker.py` with:

```python
def test_worker_health_reports_project_execution_only(tmp_path, monkeypatch):
    from flight_agent.matlab import engine_available

    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    with TestClient(worker().create_worker_app(tmp_path, TOKEN)) as client:
        assert client.get("/health").status_code == 401
        assert client.get("/health", headers=HEADERS).json() == {
            "service": "matlab-worker",
            "engine_package": engine_available(),
            "trusted_code_only": True,
            "project_execution": True,
        }


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/workbench/runs"),
        ("POST", "/workbench/runs"),
        ("GET", "/workbench/runs/" + "a" * 32),
        (
            "GET",
            "/workbench/runs/"
            + "a" * 32
            + "/artifacts/figure-1.png",
        ),
    ],
)
def test_worker_rejects_removed_workbench_routes(
    tmp_path, monkeypatch, method, path
):
    monkeypatch.setenv("MATLAB_ALLOW_TRUSTED_PROJECTS", "1")
    with TestClient(worker().create_worker_app(tmp_path, TOKEN)) as client:
        response = client.request(method, path, headers=HEADERS, json={})
        assert response.status_code == 404
```

- [ ] **Step 2: Run the Worker tests and verify RED**

Run:

```bash
TMPDIR=/private/tmp uv run pytest \
  tests/test_verification_worker.py::test_worker_health_reports_project_execution_only \
  tests/test_verification_worker.py::test_worker_rejects_removed_workbench_routes -q
```

Expected: health includes `single_file_execution` and workbench routes respond
instead of returning 404.

- [ ] **Step 3: Remove Worker registration and health capability**

In `src/flight_agent/worker.py`:

- remove the `workbench_routes` import;
- remove `app.include_router(workbench_routes(root, runner))`;
- remove `"single_file_execution": True` from health.

Keep the single shared `BackgroundRunner` for project routes and preserve
`MATLAB_ALLOW_TRUSTED_PROJECTS=1`.

- [ ] **Step 4: Delete the workbench execution files**

Delete the contracts, store, executor, Worker router, fixed MATLAB wrapper, and
their four dedicated test modules listed above.

Do not modify `src/flight_agent/matlab.py`; its Windows process identity and
fail-closed cleanup behavior are shared by supported execution paths.

- [ ] **Step 5: Remove the fake Engine workbench seam**

In `tests/conftest.py`, remove the injected `_workbench` function and the
`flight_agent_execute_workbench` branches added to fake Engine `feval` and
`which`. Restore delegation to the original fake Engine implementation for all
remaining calls. Keep the fixture name and every project/local MATLAB behavior
unchanged.

- [ ] **Step 6: Run Worker and MATLAB regressions**

Run:

```bash
TMPDIR=/private/tmp uv run pytest tests/test_verification_worker.py \
  tests/test_verification_matlab.py tests/test_local_matlab.py -q
uv run ruff check src/flight_agent/worker.py tests/test_verification_worker.py \
  tests/conftest.py
uv run mypy src/flight_agent/worker.py src/flight_agent/matlab.py
git diff --check
```

Expected: all commands exit 0, including existing Windows cleanup unit tests.

- [ ] **Step 7: Commit the Worker removal**

```bash
git add -A src/flight_agent/worker.py src/flight_agent/workbench_contracts.py \
  src/flight_agent/verification src/flight_agent/matlab_support \
  tests/conftest.py tests/test_verification_worker.py \
  tests/test_workbench_contracts.py tests/test_workbench_store.py \
  tests/test_workbench_matlab.py tests/test_workbench_worker.py
git commit -m "refactor: remove MATLAB workbench execution"
```

---

### Task 3: Remove examples, package data, documentation, and obsolete plans

**Files:**
- Create: `tests/test_removed_workbench.py`
- Delete: `examples/web_script.m`
- Delete: `examples/web_function.m`
- Delete: `src/flight_agent/examples/web_script.m`
- Delete: `src/flight_agent/examples/web_function.m`
- Delete: `docs/superpowers/specs/2026-11-02-matlab-single-file-workbench-design.md`
- Delete: `docs/superpowers/plans/2026-11-02-matlab-single-file-workbench.md`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `docs/usage.md`
- Modify: `docs/architecture.md`
- Verify unchanged: `.github/workflows/ci.yml`
- Verify unchanged: `tests/test_ci_contract.py`

**Interfaces:**
- Consumes: the absence of all production workbench modules from Tasks 1-2.
- Produces: package and documentation that advertise only supported Agent and project flows.

- [ ] **Step 1: Write failing source/package/documentation absence tests**

Create `tests/test_removed_workbench.py`:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REMOVED_PATHS = (
    "examples/web_script.m",
    "examples/web_function.m",
    "src/flight_agent/examples/web_script.m",
    "src/flight_agent/examples/web_function.m",
    "src/flight_agent/static/matlab-workbench.html",
    "src/flight_agent/static/matlab-workbench.css",
    "src/flight_agent/static/matlab-workbench.js",
    "src/flight_agent/matlab_support/flight_agent_execute_workbench.m",
    "src/flight_agent/workbench_contracts.py",
    "src/flight_agent/web_routes/workbench.py",
    "src/flight_agent/verification/workbench_store.py",
    "src/flight_agent/verification/workbench_matlab.py",
    "src/flight_agent/verification/workbench_worker.py",
)


def test_workbench_source_and_resources_are_removed():
    for relative in REMOVED_PATHS:
        assert not (ROOT / relative).exists(), relative


def test_user_documentation_has_no_workbench_surface():
    for relative in ("README.md", "docs/usage.md", "docs/architecture.md"):
        text = (ROOT / relative).read_text("utf-8").lower()
        assert "/workbench" not in text, relative
        assert "matlab-workbench" not in text, relative
        assert "web_script.m" not in text, relative
        assert "web_function.m" not in text, relative


def test_package_data_has_no_workbench_examples():
    text = (ROOT / "pyproject.toml").read_text("utf-8")
    assert "examples/*.m" not in text
```

- [ ] **Step 2: Run the removal contract and verify RED**

Run:

```bash
uv run pytest tests/test_removed_workbench.py -q
```

Expected: failures list the remaining examples, package-data entry, and
documentation references.

- [ ] **Step 3: Remove examples and package data**

Delete both repository examples and both packaged copies.

Change `pyproject.toml` package data from:

```toml
flight_agent = ["static/*", "matlab_support/*.m", "examples/*.m"]
```

to:

```toml
flight_agent = ["static/*", "matlab_support/*.m"]
```

- [ ] **Step 4: Remove user-facing documentation**

Delete every single-file workbench row, section, route, startup command,
security warning, storage path, component-tree entry, and example reference
from `README.md`, `docs/usage.md`, and `docs/architecture.md`.

Preserve:

- Agent homepage instructions;
- project Worker startup and trust boundary;
- project verification architecture;
- unrelated scheduler text such as the phrase “workbench continue polling” if
  it describes scheduler fixtures rather than the removed product surface.

- [ ] **Step 5: Delete obsolete restoration design and plan**

Delete:

```text
docs/superpowers/specs/2026-11-02-matlab-single-file-workbench-design.md
docs/superpowers/plans/2026-11-02-matlab-single-file-workbench.md
```

Keep:

```text
docs/superpowers/specs/2026-11-03-remove-matlab-workbench-design.md
docs/superpowers/plans/2026-11-03-remove-matlab-workbench.md
```

- [ ] **Step 6: Verify CI remains generic**

Run:

```bash
uv run pytest tests/test_ci_contract.py -q
uv run pytest --collect-only -m matlab -q
git diff --exit-code -- .github/workflows/ci.yml tests/test_ci_contract.py
```

Expected: CI contract tests pass, remaining MATLAB tests are collected, and no
CI workflow change is required.

- [ ] **Step 7: Build and inspect the wheel**

Run:

```bash
uv run python -m build --no-isolation
python - <<'PY'
from pathlib import Path
from zipfile import ZipFile

wheel = max(Path("dist").glob("*.whl"), key=lambda item: item.stat().st_mtime)
with ZipFile(wheel) as archive:
    names = archive.namelist()
for forbidden in (
    "workbench",
    "web_script.m",
    "web_function.m",
    "flight_agent_execute_workbench.m",
):
    assert all(forbidden not in name for name in names), forbidden
PY
```

Expected: build exits 0 and no workbench resource appears in the wheel.

- [ ] **Step 8: Run documentation/package checks**

Run:

```bash
uv run pytest tests/test_removed_workbench.py tests/test_ci_contract.py -q
uv run ruff check tests/test_removed_workbench.py
uv lock --check
git diff --exit-code -- uv.lock
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 9: Commit the resource and documentation removal**

```bash
git add -A README.md docs examples pyproject.toml src/flight_agent/examples \
  tests/test_removed_workbench.py
git commit -m "docs: remove MATLAB workbench support"
```

---

### Task 4: Review, verify, and publish the removal

**Files:**
- Review: all changes since `7127cb7`

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: reviewed branch with the workbench absent and all supported flows green.

- [ ] **Step 1: Request a whole-branch code review**

Review against the approved removal spec, focusing on:

- no registered Web or Worker workbench routes;
- no dormant single-file execution/import path;
- no packaged workbench resource or user-facing link;
- shared MATLAB cleanup preserved;
- homepage and project execution non-regression.

Fix every Critical and Important finding with focused RED/GREEN tests.

- [ ] **Step 2: Run focused removal and regression tests**

Run:

```bash
TMPDIR=/private/tmp uv run pytest \
  tests/test_removed_workbench.py tests/test_web_surface.py \
  tests/test_web_modules.py tests/test_frontend_modules.py \
  tests/test_verification_worker.py tests/test_verification_matlab.py \
  tests/test_local_matlab.py -q

TMPDIR=/private/tmp RUN_BROWSER_TESTS=1 RUN_MATLAB_TESTS=0 uv run pytest \
  tests/test_agent_layout_browser.py -q
```

Expected: all commands exit 0.

- [ ] **Step 3: Run complete release gates**

Run:

```bash
TMPDIR=/private/tmp RUN_BROWSER_TESTS=0 RUN_MATLAB_TESTS=0 \
  MATLAB_ALLOW_TRUSTED_PROJECTS=0 \
  uv run pytest -m "not matlab and not browser" -q

TMPDIR=/private/tmp RUN_BROWSER_TESTS=1 RUN_MATLAB_TESTS=0 \
  uv run pytest -m "browser and not matlab" -q

uv run ruff check .
uv run mypy src
uv run python -m build --no-isolation
uv lock --check
git diff --exit-code -- uv.lock
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 4: Verify no production workbench references remain**

Run:

```bash
git grep -n -i workbench -- \
  src README.md docs/usage.md docs/architecture.md pyproject.toml \
  ':!docs/superpowers/specs/2026-11-03-remove-matlab-workbench-design.md' \
  ':!docs/superpowers/plans/2026-11-03-remove-matlab-workbench.md'
```

Expected: no matches.

- [ ] **Step 5: Push and verify GitHub Actions**

```bash
git push -u origin remove/matlab-workbench
gh run list --branch remove/matlab-workbench --limit 3
RUN_ID=$(gh run list --branch remove/matlab-workbench --limit 1 \
  --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN_ID" --exit-status
```

Required jobs:

- Software Python 3.11: success;
- Software Python 3.12: success;
- Browser Chromium: success;
- Licensed MATLAB acceptance: skipped unless manually requested.

- [ ] **Step 6: Verify final repository state**

```bash
git status --short --branch
git rev-parse HEAD
git rev-parse origin/remove/matlab-workbench
```

Expected: clean worktree and identical local/remote SHAs.
