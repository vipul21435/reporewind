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
  permitted in the setup session; the owner created it afterwards and it is
  now `origin` (`main`). Push fast-forward only; never force-push.
- **Planned runtime dependencies** (all light, no ML stack): typer, pydantic v2,
  pyyaml, httpx, packaging, defusedxml, fastapi, uvicorn.

- **Slice 1 decisions (models, errors, git).**
  - Git runs *hermetically*: `GIT_CONFIG_GLOBAL=/dev/null`,
    `GIT_CONFIG_NOSYSTEM=1`, inherited `GIT_DIR`/`GIT_INDEX_FILE`/`GIT_CONFIG_*`
    scrubbed, `GIT_CEILING_DIRECTORIES` set to the parent of the target so a
    non-repository work dir can never fall through to an enclosing repository,
    `GIT_ALLOW_PROTOCOL=file:git:http:https:ssh`, `GIT_TERMINAL_PROMPT=0`,
    `LC_ALL=C`. Trade-off: user `url.insteadOf` rewrites and credential
    helpers are ignored; private repositories are out of scope.
  - Diffs always use `--binary --full-index --src-prefix=a/ --dst-prefix=b/`
    and an explicit rename mode, and are decoded with `surrogateescape`, so a
    patch captured from any repository re-applies byte for byte.
  - Work dirs are linked worktrees (`git worktree add --detach`), not clones:
    one object store per repository, cheap checkouts, and they work from a
    bare cache repository. `apply` refuses to run without a work tree because
    in a bare repository `git apply` writes into the git dir.
  - Revisions, remotes, refspecs and URLs starting with `-` are rejected
    before git runs, and `--` / `--end-of-options` separate operands.
  - `RepoRef` keeps identity only (host, owner, name). Where a stage needs to
    clone from somewhere else (a local mirror, a test fixture), it takes an
    explicit source location instead of stuffing a path into `RepoRef`.
    `file://` and local paths are therefore not accepted by `RepoRef.parse`.
  - `ResolvedFix` requires both a non-empty source patch and a non-empty test
    patch, disjoint by path: a fix without test changes cannot prove a flip,
    so the resolver (slice 2) must fail with `ResolveError` instead of
    producing one.
  - The fixture builder lives in `reporewind.testing` (no pytest import, like
    `typer.testing`) so it is strictly typed and reusable; `tests/conftest.py`
    exposes it as `repo_factory`. Its initial commit SHA is pinned in a test
    to catch any machine-dependent drift.

- **Slice 2 decisions (resolver, diff splitter, PR lookup).**
  - The split is proven in a *scratch index* (`git apply --cached` with a
    temporary `GIT_INDEX_FILE`), not a worktree: no checkout, nothing
    written to the user's clone except objects, and it works in the bare
    cache. Source and test patches must each apply to the base on their
    own, and base + source + test must equal the fix commit's tree id, which
    proves the split lost and duplicated nothing.
  - A patch is a test change only if *every* path it touches is a test
    path, so a rename across the test/source boundary lands in the source
    patch and the test patch never edits library code. Default test dirs
    are `tests` and `test` only (`testing/` is often library code, e.g. a
    package's public test helpers); `tests.py` (Django style) joins the
    spec's `test_*.py`, `*_test.py` and `conftest.py`.
  - The diff parser takes paths from rename/copy headers, then `---`/`+++`,
    then the same-name `diff --git a/P b/P` form; it rejects `Binary files
    differ` stubs (not re-applicable), unknown header lines and unsafe
    paths, and merges git's delete+add pair for a type change into one
    patch so each path appears once.
  - `commit_info` reads parents from the raw commit object: `%P` reports no
    parents for a shallow-fetch boundary, which made the base look like a
    root commit and made the JSON depend on fetch depth.
    `GIT_NO_REPLACE_OBJECTS=1` joins the hermetic environment.
  - The repo cache is one bare repository per hosted repository under
    `$REPOREWIND_HOME/repos/<host>/<owner>__<repo>.git`, filled by
    `fetch --depth 2 <sha>` and pinned under `refs/reporewind/commits/`.
    A commit that is only a shallow boundary is fetched again (deepened).
    There is no cross-process lock yet; the build lock arrives in slice 5
    and concurrent fetches into one cache rely on git's own locking.
  - PR shapes: two parents -> mainline 1; one parent -> the landed commit;
    a multi-commit rebase merge (landed commit has the same message and
    author date as the PR's last commit) is rejected, since no single
    commit is the whole fix. PRs above GitHub's 250-commit listing limit
    are rejected for the same check. The API's own parent list picks the
    base, and git re-derives it from the fetched commit.
  - `pydantic`'s `model_dump_json` rejects the lone surrogates that carry
    non-UTF-8 diff bytes, so `ResolvedFix` JSON always goes through
    `dump_resolved_fix` / `load_resolved_fix` (ASCII JSON with `\udcXX`
    escapes). The bundle writer in slice 7 must use them too.
  - Offline GitHub tests replay responses recorded from the live API
    (pallets/markupsafe#477, a merge commit; python-attrs/attrs#1606, a
    squash of two commits), trimmed by `tests/fixtures/github/record.sh`;
    rarer shapes and error statuses are synthetic variants built in the
    test. `reporewind.cli.github_client` is the seam tests patch.
  - `reporewind.testing.make_bugfix` (pinned SHAs) is the shared bug-fix
    repository for the verify slice: its new test fails without the fix.

- **Delivery pass decisions (demo and image, ahead of slice 8).**
  - `make demo` needs no network: `demo/slugkit.fi` is a `git fast-import`
    stream generated by `demo/make_sample.py` with `RepoFactory` (pinned
    SHAs), excluded from the whitespace hooks so it stays byte-exact, and a
    test regenerates it and compares bytes. The demo's fail-to-pass step is
    plain `git apply` + `unittest` in the script, clearly labelled as manual
    until slice 6 automates it.
  - The CLI image is two-stage (uv and `python:3.12-slim`, both pinned by
    digest), runs as uid 10001 and needs git at runtime; static tests check
    the pins, the user, the label and the build context, and a CI job builds
    the image and runs the demo in it. Slice 8 still owns compose, the API
    image, the e2e CI job and committed bundles for public repositories.

- **Slice 3 decisions (recipes).**
  - A `Recipe` holds only values that change the environment, normalized so
    equivalent recipes hash equal (canonical `SpecifierSet`, PEP 685 extras,
    PEP 508 requirement strings, sorted system packages and env). The hash
    is `sha256:` of `{"schema_version": 1, "recipe": ...}` as sorted,
    compact, ASCII JSON; a test pins the default recipe's hash.
  - Recipe files keep two layers: `detected` (commit, backend, sources,
    notes, and a *sparse* recipe with only the fields that had evidence) and
    hand-written `overrides`. Sparse detection matters: an override of
    `test_framework` then brings that framework's default command instead
    of keeping the detected one. Overrides use JSON Merge Patch (RFC 7396)
    rather than an ad-hoc deep merge, so `null` has one documented meaning.
  - Detection reads blobs with `git cat-file` at the base commit (the
    plumbing form of `git show REV:PATH`, without textconv), so it works in
    the bare cache and ignores the work tree. `setup.py` is only parsed with
    `ast`: literals, module-level names bound to literals, and `+` of lists
    or strings; everything else becomes a note.
  - Precedence for the Python constraint: `[project] requires-python`,
    Poetry's `python`, legacy flit metadata, `setup.cfg`, `setup.py`;
    disagreements are noted. Test dependencies come from PEP 735 groups
    (`test`/`tests`/`testing`, else `dev`), Poetry groups, hatch envs,
    `tests_require` and tox `deps`; extras from tox (`extras`, `.[x]`) plus
    test-named extras. Requirements files: root `requirements.txt`, then
    test files (or dev/ci files if there are none), plus tox `-r` files;
    docs, lint and typing files are skipped.
  - Framework: pytest with any evidence (config sections, `pytest.ini`,
    `conftest.py`, tox commands, test files importing pytest or defining
    plain `test_` functions), unittest with only unittest evidence, pytest
    by default. If pytest is chosen but declared nowhere it is added
    unpinned; slice 4 dates it with `--exclude-newer`.
  - Backends without PEP 660 editable installs (`poetry.masonry.api`,
    `flit.buildapi`) get `install: package`; C/Cython sources or an
    `Extension` add `build-essential`; Rust backends only get a note.
  - Known gap for slice 5: projects whose version comes from VCS metadata
    (attrs uses `hatch-vcs`) need the `.git` directory or
    `SETUPTOOLS_SCM_PRETEND_VERSION` in the image; detection does not flag
    this yet.
  - `recipes/pallets__markupsafe.yaml` and `recipes/python-attrs__attrs.yaml`
    are real detector output at the bases of markupsafe#477 and attrs#1606;
    an offline test validates every committed recipe and an `e2e` test
    re-detects them from GitHub and compares the `detected` layer.

- **Slice 4 decisions (pinning).**
  - The pinned commit is the one that gets built (the fix's parent, or
    `--at-rev`), and "the commit date" is its *committer* date (when it
    landed), compared in UTC.
  - Python choice: supported minor versions (3.7 to 3.14, which have
    `python:X.Y-slim` images and resolver support) whose `X.Y.0` was out
    on that day, filtered by the recipe's constraint; a minor version
    satisfies it if its `.0` or a late patch release does (`>=3.8.1`
    admits 3.8). Classifiers narrow the choice only when they intersect
    the candidates (attrs lists 3.15, unreleased on its commit date, so it
    never becomes a candidate); otherwise a note says they were ignored. A commit older than
    3.7 falls back to 3.7 with a note; a constraint nothing released
    satisfies is a `PinError`. `--python` wins but conflicts become notes.
  - Classifiers and runtime requirements are read by a separate static
    reader (`pinning.metadata`) rather than stored in the recipe: the
    recipe holds environment choices a human reviews, while these are
    facts of the commit. The `setup.py` literal readers moved to
    `recipes.static` so both readers share them.
  - Lock inputs: runtime requirements (plus the recipe's extras) only for
    `editable`/`package` installs, the recipe's test dependencies, and the
    requirements files copied from the tree at their repository paths so
    relative `-r`/`-c` includes resolve (depth limit 10, paths outside the
    repository dropped with a note). Lines that install the project itself
    (`-e .`, local paths, `file:`) are dropped, so the host never runs the
    project's build backend; slice 5 installs the project with
    `--no-deps` on top of the lock.
  - uv runs with `--no-config`, `--no-header`, `UV_*` variables scrubbed
    (except `UV_CACHE_DIR`), and in a scratch directory, so the `# via`
    annotations are stable relative paths and the lock is reproducible. A
    two-line reporewind header records the interpreter, platform and
    cutoff; the lock's sha256 goes into the `PinResult`. Hashes are on by
    default; the platform defaults to `x86_64-unknown-linux-gnu`.
  - Base image digests are the multi-platform *index* digest from
    `docker buildx imagetools inspect --format '{{json .Manifest}}'` (no
    pull). Any lookup failure falls back to a table recorded on 2026-09-29
    with a note in the result; offline mode never calls docker. A test
    ties the table's 3.12 entry to the digest pinned in `Dockerfile`.
  - `pin.json` and `requirements.lock` default to
    `$REPOREWIND_HOME/pins/<host>/<owner>__<repo>/<sha>/`; they are build
    inputs, not reviewed files, so they are not committed like recipes.
    Unit tests use a scripted `CommandRunner` (`tests/fakes.py`); two
    `e2e` tests run the real uv resolver and registry lookup.

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

1. [x] **Domain models, errors and git plumbing** (done 2026-09-29) - Add pydantic v2 domain models (`RepoRef` parsed from HTTPS/SSH/`owner/repo` forms, `CommitInfo`, `FilePatch`, `ResolvedFix`), a typed error hierarchy, and a no-shell `Git` wrapper (clone, fetch a single SHA, rev-parse, parents, commit date, diff, apply --check, apply, detached checkout into a work dir). Ship a pytest `repo_factory` fixture that builds throwaway git repos in tmp dirs with fixed author/committer dates, so every later slice tests offline.

2. [x] **Commit resolver and diff splitter** (done 2026-09-29) - From a repo URL plus fix SHA, resolve the parent commit (reject root commits; require an explicit mainline for merge commits), compute the fix diff and split it per file into a source patch and a test patch using configurable path rules (`tests/`, `test_*.py`, `*_test.py`, `conftest.py`), handling adds, deletes, renames and binary files, and prove both patches apply cleanly to the parent. Resolve a GitHub PR number to its fix/base commits through the REST API (httpx with an injectable transport, recorded JSON fixtures, optional `GITHUB_TOKEN`). Expose `reporewind resolve` that prints or writes the `ResolvedFix` JSON.

3. [x] **Build recipes: schema, detection and store** (done 2026-09-29) - Define a pydantic `Recipe` (Python constraint, install mode, extras, requirements files, system packages, pre-install steps, test command, test framework, env) and detectors that read `pyproject.toml` (PEP 621, setuptools, poetry, hatch, flit), `setup.py` (parsed with `ast`, never executed), `setup.cfg`, `requirements*.txt` and `tox.ini` at the parent commit via `git show`. Store recipes as `recipes/<owner>__<repo>.yaml` where human overrides deep-merge over detected values and the result is validated; add a stable recipe hash (sha256 of canonical JSON) and `reporewind recipe detect|show|validate`.

4. [x] **Historical pinning: Python version, dependencies, base image** (done 2026-09-29) - Infer the Python version from `requires-python`, trove classifiers and a commit-date table of CPython release dates (newest release that existed at the commit and satisfies the constraints). Resolve dependencies as of the commit date with `uv pip compile --exclude-newer <commit-date> --python-version X.Y` behind a `CommandRunner` protocol (fake in unit tests). Resolve `python:X.Y-slim` to an immutable `@sha256:` digest via `docker buildx imagetools inspect`, with an offline fallback table and `REPOREWIND_OFFLINE`. Expose `reporewind pin`, which writes the lock file and a `PinResult`.

5. [ ] **Dockerfile generation, build cache and build lock** - Render a deterministic Dockerfile (base image by digest, non-root user, locked requirements installed with uv, repository checked out at the parent SHA, no network needed at test time) with golden-file tests. Tag images by a content hash of recipe + lock + base digest, keep a JSON build-cache index so an identical environment is never rebuilt, and guard builds with a cross-process file lock (timeout, stale-lock recovery) proven by a multiprocessing test in which N concurrent builders produce exactly one build. Expose `reporewind build` with `--dry-run`.

6. [ ] **Test runner, JUnit parsing and fail-to-pass verification** - Parse JUnit XML safely (defusedxml; pytest and unittest variants, failure vs error vs skip, parametrized ids, nested testsuites) into per-test outcomes. Add an `Executor` protocol with a local subprocess executor (used by integration tests on throwaway repos) and a Docker executor. Implement the flip protocol: parent + test patch must fail the selected tests, parent + fix + test patch must pass them; compute FAIL_TO_PASS and PASS_TO_PASS and a `Verdict` (valid, no-flip, broken-baseline, regression). Detect flaky tests by re-running N times and excluding inconsistent ones with a report. Expose `reporewind verify`.

7. [ ] **Task bundle export, full CLI and FastAPI service** - Write task bundles (`task.json` validated by a pydantic model whose JSON Schema is exported to `schemas/`, `Dockerfile`, `fix.patch`, `test.patch`, `fail_to_pass.txt`, `pass_to_pass.txt`, the dependency lock, and a sha256 manifest) and a `reporewind validate-bundle` command that re-checks schema and hashes. Wire the end-to-end `reporewind run` (resolve, recipe, pin, build, verify, export) with consistent exit codes, and add a small FastAPI service (`GET /healthz`, `POST /resolve`, `POST /jobs`, `GET /jobs/{id}`) backed by a background job queue, tested with `TestClient`.

8. [ ] **Docker, compose and end-to-end demo** - Add a slim multi-stage Dockerfile for the RepoRewind CLI/API (non-root, labeled `project=reporewind`), a `docker-compose.yml` for the API with a cache volume and opt-in Docker socket, `e2e`-marked tests that exercise the real network and Docker path, and a separate manually triggered CI job for them. Make `make demo` run the full pipeline on 1-2 small, permissively licensed public Python repos at real fix commits, and commit the produced bundles and verification logs under `demo/` with the exact commands used.

9. [ ] **Benchmarks and docs polish** - Add a benchmark script under `bench/` that measures diff-split and JUnit-parse throughput, cold vs warm (cache-hit) environment build time and end-to-end verify time on the demo repos, and put its output in the README with the command to reproduce it. Write `docs/` pages for the architecture (ASCII diagram), recipe format and bundle schema; finish the README with real coverage and test counts from `make cov`, limitations and a roadmap; add a CHANGELOG.

## Definition of done (every slice)

- `make lint typecheck cov` is green; coverage stays at or above 85%.
- New behavior has unit tests; anything touching git uses `repo_factory`.
- Numbers in docs come from a command that was actually run, with that command.
- Conventional Commits, ASCII only, no secrets, no client names or client data.
