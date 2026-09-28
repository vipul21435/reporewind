# RepoRewind - implementation plan

RepoRewind rebuilds an open-source Python repository at a historical commit
inside a digest-pinned Docker image and proves a fail-to-pass flip for a fix
commit, then exports the result as a schema-validated task bundle.

## Decisions

- **Built fresh, not forked.** The existing open-source harnesses in this space
  are large (well above 15k LOC) evaluation frameworks; forking one would bury
  the original work and pull in heavy dependencies. The spec is narrow enough
  that a clean, typed implementation is smaller and easier to review.
- **Offline by default.** Every unit and integration test builds throwaway git
  repositories inside pytest tmp dirs. Anything that needs the network or a
  Docker daemon is marked `e2e` and excluded from the default run
  (`addopts = -m 'not e2e'`); `make e2e` runs it.
- **External tools behind protocols.** `git`, `uv`, `docker` and the GitHub API
  are reached through small typed adapters (a `CommandRunner` protocol, an
  httpx client with an injectable transport) so unit tests use fakes and
  recorded fixtures instead of the real tools.
- **Never execute repository code during detection.** `setup.py` is read with
  `ast`, never imported or run. Repository code only runs inside the build
  container or the local test executor.
- **Test identity.** Tests set `GIT_AUTHOR_*` / `GIT_COMMITTER_*` and fixed
  dates through the environment so they pass on machines without a global git
  config (CI included) and produce stable SHAs.
- **Remote.** Creating the GitHub repository `vipul21435/reporewind` was not
  permitted in the setup session, so the scaffold commits are local only. The
  owner creates the remote; later agents must not try to work around that and
  should keep committing locally until `origin` exists.
- **Planned runtime dependencies** (all light, no ML stack): typer, pydantic v2,
  pyyaml, httpx, packaging, defusedxml, fastapi, uvicorn.

## Target layout

```
src/reporewind/
  errors.py          typed error hierarchy
  models.py          shared pydantic domain models
  gitops.py          no-shell git wrapper
  resolve/           commit resolver, diff splitter, GitHub PR lookup
  recipes/           recipe schema, detectors, YAML store with overrides
  pinning/           python version inference, uv lock, base image digests
  build/             Dockerfile renderer, builder, cache index, build lock
  verify/            JUnit parser, executors, flip protocol, flaky detection
  bundle/            task bundle writer, JSON schema, validator
  api/               FastAPI service
  cli.py             Typer CLI
recipes/             <owner>__<repo>.yaml recipes with human overrides
schemas/             exported JSON Schemas (task.json, recipe)
demo/                recorded end-to-end runs on public repos
```

## Slices

Each slice is a coherent feature delivered as 3-4 real commits, each with
tests, green lint (ruff), strict typecheck (mypy) and a green suite.

1. **Domain models, errors and git plumbing** - Add pydantic v2 domain models (`RepoRef` parsed from HTTPS/SSH/`owner/repo` forms, `CommitInfo`, `FilePatch`, `ResolvedFix`), a typed error hierarchy, and a no-shell `Git` wrapper (clone, fetch a single SHA, rev-parse, parents, commit date, diff, apply --check, apply, detached checkout into a work dir). Ship a pytest `repo_factory` fixture that builds throwaway git repos in tmp dirs with fixed author/committer dates, so every later slice tests offline.

2. **Commit resolver and diff splitter** - From a repo URL plus fix SHA, resolve the parent commit (reject root commits; require an explicit mainline for merge commits), compute the fix diff and split it per file into a source patch and a test patch using configurable path rules (`tests/`, `test_*.py`, `*_test.py`, `conftest.py`), handling adds, deletes, renames and binary files, and prove both patches apply cleanly to the parent. Resolve a GitHub PR number to its fix/base commits through the REST API (httpx with an injectable transport, recorded JSON fixtures, optional `GITHUB_TOKEN`). Expose `reporewind resolve` that prints or writes the `ResolvedFix` JSON.

3. **Build recipes: schema, detection and store** - Define a pydantic `Recipe` (Python constraint, install mode, extras, requirements files, system packages, pre-install steps, test command, test framework, env) and detectors that read `pyproject.toml` (PEP 621, setuptools, poetry, hatch, flit), `setup.py` (parsed with `ast`, never executed), `setup.cfg`, `requirements*.txt` and `tox.ini` at the parent commit via `git show`. Store recipes as `recipes/<owner>__<repo>.yaml` where human overrides deep-merge over detected values and the result is validated; add a stable recipe hash (sha256 of canonical JSON) and `reporewind recipe detect|show|validate`.

4. **Historical pinning: Python version, dependencies, base image** - Infer the Python version from `requires-python`, trove classifiers and a commit-date table of CPython release dates (newest release that existed at the commit and satisfies the constraints). Resolve dependencies as of the commit date with `uv pip compile --exclude-newer <commit-date> --python-version X.Y` behind a `CommandRunner` protocol (fake in unit tests). Resolve `python:X.Y-slim` to an immutable `@sha256:` digest via `docker buildx imagetools inspect`, with an offline fallback table and `REPOREWIND_OFFLINE`. Expose `reporewind pin`, which writes the lock file and a `PinResult`.

5. **Dockerfile generation, build cache and build lock** - Render a deterministic Dockerfile (base image by digest, non-root user, locked requirements installed with uv, repository checked out at the parent SHA, no network needed at test time) with golden-file tests. Tag images by a content hash of recipe + lock + base digest, keep a JSON build-cache index so an identical environment is never rebuilt, and guard builds with a cross-process file lock (timeout, stale-lock recovery) proven by a multiprocessing test in which N concurrent builders produce exactly one build. Expose `reporewind build` with `--dry-run`.

6. **Test runner, JUnit parsing and fail-to-pass verification** - Parse JUnit XML safely (defusedxml; pytest and unittest variants, failure vs error vs skip, parametrized ids, nested testsuites) into per-test outcomes. Add an `Executor` protocol with a local subprocess executor (used by integration tests on throwaway repos) and a Docker executor. Implement the flip protocol: parent + test patch must fail the selected tests, parent + fix + test patch must pass them; compute FAIL_TO_PASS and PASS_TO_PASS and a `Verdict` (valid, no-flip, broken-baseline, regression). Detect flaky tests by re-running N times and excluding inconsistent ones with a report. Expose `reporewind verify`.

7. **Task bundle export, full CLI and FastAPI service** - Write task bundles (`task.json` validated by a pydantic model whose JSON Schema is exported to `schemas/`, `Dockerfile`, `fix.patch`, `test.patch`, `fail_to_pass.txt`, `pass_to_pass.txt`, the dependency lock, and a sha256 manifest) and a `reporewind validate-bundle` command that re-checks schema and hashes. Wire the end-to-end `reporewind run` (resolve, recipe, pin, build, verify, export) with consistent exit codes, and add a small FastAPI service (`GET /healthz`, `POST /resolve`, `POST /jobs`, `GET /jobs/{id}`) backed by a background job queue, tested with `TestClient`.

8. **Docker, compose and end-to-end demo** - Add a slim multi-stage Dockerfile for the RepoRewind CLI/API (non-root, labeled `project=reporewind`), a `docker-compose.yml` for the API with a cache volume and opt-in Docker socket, `e2e`-marked tests that exercise the real network and Docker path, and a separate manually triggered CI job for them. Make `make demo` run the full pipeline on 1-2 small, permissively licensed public Python repos at real fix commits, and commit the produced bundles and verification logs under `demo/` with the exact commands used.

9. **Benchmarks and docs polish** - Add a benchmark script under `bench/` that measures diff-split and JUnit-parse throughput, cold vs warm (cache-hit) environment build time and end-to-end verify time on the demo repos, and put its output in the README with the command to reproduce it. Write `docs/` pages for the architecture (ASCII diagram), recipe format and bundle schema; finish the README with real coverage and test counts from `make cov`, limitations and a roadmap; add a CHANGELOG.

## Definition of done (every slice)

- `make lint typecheck cov` is green; coverage stays at or above 85%.
- New behavior has unit tests; anything touching git uses `repo_factory`.
- Numbers in docs come from a command that was actually run, with that command.
- Conventional Commits, ASCII only, no secrets, no client names or client data.
