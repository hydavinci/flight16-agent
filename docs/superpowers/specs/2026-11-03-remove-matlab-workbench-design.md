# Remove the MATLAB Single-File Workbench

## Context

The repository currently contains a dedicated `/workbench` product surface for
uploading and executing one trusted MATLAB `.m` file through the configured
Worker. The product no longer needs this capability. The removal must be
complete: there must be no page, link, Web API, Worker API, packaged example,
or dormant execution subsystem left behind.

The generic Agent homepage and the existing project-oriented MATLAB/Simulink
analysis flow remain supported.

## Goals

- Remove the `/workbench` page and every visible link to it.
- Make `/workbench`, `/api/workbench/*`, and Worker `/workbench/*` return 404.
- Remove all single-file workbench contracts, persistence, execution, wrapper,
  examples, package resources, browser assets, and dedicated tests.
- Remove workbench-specific documentation, architecture text, and release
  acceptance coverage.
- Preserve the homepage, project verification routes, and existing MATLAB
  project execution behavior.

## Non-goals

- Do not remove the generic Agent homepage.
- Do not remove Worker project verification or local/project MATLAB execution.
- Do not revert the shared Windows MATLAB process-tree cleanup improvements in
  `flight_agent.matlab`; they protect non-workbench execution paths too.
- Do not weaken the generic `pytest -m matlab` CI selector merely because the
  workbench-specific live tests are removed.
- Do not redirect `/workbench` to `/`; absence must be explicit through 404.

## Removal Architecture

### Web surface

Delete the workbench Web router, page route, RemoteClient methods, static
HTML/CSS/JavaScript, static allowlist entries, bootstrap/API wiring, and
packaged example-resource lookup. No replacement route is registered.

After removal:

- `GET /workbench` returns 404;
- every `/api/workbench/*` request returns 404;
- `/` continues to serve the Agent homepage;
- no homepage or documentation link advertises a workbench.

If `RemoteHTTPError` or another helper has no non-workbench consumer after the
methods are deleted, remove it as dead code while preserving existing project
remote error messages and retry behavior.

### Worker surface

Delete the Worker workbench router, `WorkbenchRunStore`, single-file executor,
fixed MATLAB wrapper, and workbench contracts. Remove router registration and
the `single_file_execution` health field.

After removal:

- every Worker `/workbench/*` request returns 404;
- project routes continue to use the existing shared `BackgroundRunner`;
- existing project execution and restart-recovery behavior are unchanged.

### Resources and packaging

Delete:

- repository examples `examples/web_script.m` and `examples/web_function.m`;
- packaged copies under `src/flight_agent/examples/`;
- workbench package-data declarations;
- workbench UI assets;
- the obsolete workbench restoration spec and implementation plan.

The new removal design and its implementation plan remain as the historical
record explaining why the subsystem disappeared.

### Shared code preserved

Keep the process cleanup changes in `src/flight_agent/matlab.py`, including
Windows process identity checks and fail-closed uncertainty reporting. These
are shared safety behavior used by supported MATLAB execution paths.

Remove workbench-only fake Engine behavior from `tests/conftest.py`, but retain
all seams required by project verification and local MATLAB tests.

## Documentation and CI

Remove workbench startup commands, trust warnings, route descriptions, example
instructions, and component diagrams from `README.md`, `docs/usage.md`, and
`docs/architecture.md`.

Remove the three workbench-specific licensed MATLAB acceptance tests. Keep the
manual licensed job selecting all remaining `matlab` tests so future MATLAB
coverage is collected automatically.

## Testing Strategy

### Removal contracts

Add or update focused tests proving:

- `/workbench` returns 404 and `/` still serves `Flight Agent`;
- `/api/workbench/bootstrap`, `/api/workbench/runs`, and representative nested
  workbench API paths return 404;
- Worker `/workbench/runs` and nested artifact paths return 404;
- health no longer contains `single_file_execution`;
- static workbench asset names are not allowlisted;
- wheel contents contain no workbench assets, wrapper, contracts, or examples;
- source and documentation scans contain no user-facing workbench link or
  startup guidance outside the removal design/plan.

### Regression gates

Run:

- focused Web, Worker, remote, packaging, and project MATLAB tests;
- the complete non-MATLAB/non-browser software suite;
- the complete browser suite;
- Ruff, mypy, build, lock-file, and diff checks.

Licensed MATLAB does not need a new removal-specific run because the removed
single-file path is no longer executable. Existing licensed project tests
remain governed by the unchanged opt-in CI job.

## Migration and Operational Notes

Existing Worker directories under `runs/workbench-worker/` are not read after
the removal. The application does not delete historical run data
automatically; administrators may remove it according to their normal
retention policy.

Any already-running Web process must be restarted from the updated branch or
installed package before the removed routes disappear. Removing source code
does not mutate a process that was started from an older checkout.

## Acceptance Criteria

- No registered workbench route or visible link remains.
- All former Web and Worker workbench endpoints return 404.
- No single-file source execution code or package resource remains.
- Homepage and project-oriented MATLAB behavior pass their existing tests.
- Complete software/browser/static/build gates pass.
