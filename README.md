# RepoRewind

Rebuild an open-source Python repository at any historical commit inside a
digest-pinned Docker image, then prove that a fix commit flips its tests from
failing to passing.

> Status: early. The scaffold (packaging, lint, strict typing, tests, CI),
> the foundations (domain models, typed errors, a hermetic git layer and an
> offline repository fixture) and the first pipeline stage (resolve) are in
> place; the remaining stages are being built slice by slice, tracked in
> [PLAN.md](PLAN.md). Every stage is marked with its current state so nothing
> here claims more than the code does.

## Why this exists

Benchmarks for AI coding agents are only as trustworthy as their environments.
A task built from a real bug fix is useful only if:

- the repository builds **exactly** as it did on the day of the fix, not with
  whatever dependency versions are current today;
- the tests that the fix touches **fail** on the parent commit and **pass**
  after the fix (FAIL_TO_PASS), while the rest keep passing (PASS_TO_PASS);
- the result is **reproducible**: pinned base-image digests, pinned
  dependencies, and a bundle anyone can rebuild and re-verify.

RepoRewind automates that loop. It grew out of the author's benchmark-task
work at an AI-data company (historical-commit environments, verifier images,
per-repo build recipes, build locks and caches) and is written from scratch as
an open, generic tool. No client tasks or data are included.

## Pipeline

| Stage | What it does | State |
|---|---|---|
| Resolve | Repo URL + fix SHA (or PR number) -> parent commit; split the diff into source vs test patches | **implemented** |
| Recipe | Detect how to install and test the repo (pyproject, setup.py, setup.cfg, requirements, tox); YAML recipes with human overrides | planned |
| Pin | Resolve dependencies as of the commit date (`uv pip compile --exclude-newer`), infer the Python version, pin the base image by digest | planned |
| Build | Generate a Dockerfile, build it once per recipe hash behind a build lock | planned |
| Verify | Run tests on parent+test patch (must fail) and fix+test patch (must pass); parse JUnit XML; FAIL_TO_PASS / PASS_TO_PASS; flaky detection | planned |
| Export | Schema-validated task bundle: `task.json`, Dockerfile, patches, test lists | planned |

## Resolve a fix commit (implemented)

`reporewind resolve` turns a fix commit, or a merged GitHub pull request,
into a `ResolvedFix`: the fix, its base commit, and the diff split per file
into a **source patch** (the fix an agent would have to write) and a **test
patch** (what proves it). Real output from the two public pull requests the
test fixtures were recorded from:

```console
$ reporewind resolve pallets/markupsafe --pr 477 -o markupsafe-477.json
pull request #477 (relax speedups str check) landed as a merge-commit e85aff4d878a
resolved pallets/markupsafe fix e85aff4d878a (relax speedups str check (#477))
  base    9c44ecf45141 (parent 1 of 2)
  source  3 files: CHANGES.rst, pyproject.toml, src/markupsafe/_speedups.c
  tests   1 file: tests/test_escape.py
  proof   source and test patches each apply cleanly to base; base + both == fix tree b6ce5f96a609

$ reporewind resolve python-attrs/attrs --pr 1606 -o attrs-1606.json
pull request #1606 (Stop evolve dunders from being modified) landed as a single-commit f53fc5440d7f
resolved python-attrs/attrs fix f53fc5440d7f (Stop evolve dunders from being modified (#1606))
  base    f38b8a3f1625 (parent 1 of 1)
  source  2 files: changelog.d/1606.change.md, src/attr/_make.py
  tests   1 file: tests/test_functional.py
  proof   source and test patches each apply cleanly to base; base + both == fix tree c078cc64795b

$ jq '{pr_number, fix: .fix.sha, base: .base.sha,
       tests: [.test_patches[] | {path, change}]}' attrs-1606.json
{
  "pr_number": 1606,
  "fix": "f53fc5440d7f86aac4328aec7a563eb48634177f",
  "base": "f38b8a3f1625060aa4245930822c34c11c252f83",
  "tests": [
    {
      "path": "tests/test_functional.py",
      "change": "modified"
    }
  ]
}
```

Other forms: `reporewind resolve OWNER/REPO <full-sha>` (add `-m 1` for a
merge commit), or `--repo-dir PATH` to read an existing local clone, which
also accepts short SHAs and branch names. The JSON goes to stdout or
`--output`; the summary goes to stderr (`--quiet` drops it).

How it works:

- **Base selection.** An ordinary commit is measured against its only
  parent. A merge commit needs an explicit `--mainline` (1-based, like
  `git cherry-pick -m`), and a root commit is rejected: it has nothing to
  rewind to.
- **Per-file split.** `git diff --binary --full-index` is parsed into one
  patch per file, including adds, deletes, renames, mode changes, empty
  files, binary files, quoted non-ASCII paths and file-to-symlink type
  changes. A file is a test file if it sits under a `tests/` or `test/`
  directory or its name matches `test_*.py`, `*_test.py`, `tests.py` or
  `conftest.py`; `--test-dir`, `--test-file`, `--include` and `--exclude`
  (path globs with `*`, `**`, `?`) change the rules. A rename counts as a
  test change only if both sides are test paths, so the test patch never
  edits source code.
- **Proof.** In a scratch index (`git apply --cached` with a temporary
  `GIT_INDEX_FILE`; no checkout, no change to your clone), the source
  patch and the test patch must each apply cleanly to the base, and base +
  source + test must reproduce the fix commit's **tree id** exactly. A fix
  with no test changes, no source changes or an empty diff is rejected,
  because it cannot prove a fail-to-pass flip.
- **Pull requests.** The GitHub REST API (httpx; `GITHUB_TOKEN` optional)
  maps a merged PR to the commit that landed it: a merge commit resolves
  with mainline 1, a squash merge to its single commit. A multi-commit
  rebase merge has no single fix commit and is rejected with a message
  instead of silently resolving only its last commit.
- **Minimal fetching.** Commits are fetched by SHA with `--depth 2` into
  one bare repository per project under `$REPOREWIND_HOME/repos/` and pinned
  by a ref, so a repeat run needs no network for the git side. After the
  two runs above, `du -sh` gives 1.0M for the attrs cache and 396K for
  markupsafe, against 6.1M and 1.2M for full `git clone --bare` copies.
  Resolving from that shallow cache produces byte-identical JSON to
  resolving from a full clone (a test checks this).
- **Byte-exact JSON.** Diffs of non-UTF-8 files keep their raw bytes as
  `\udcXX` escapes in ASCII JSON, and `load_resolved_fix` restores them so
  the patch re-applies byte for byte.

Failures exit with a stable code per category: 2 for bad input (an invalid
repo reference, split rule or short SHA without `--repo-dir`), 4 for a
failed git command, 10 for a resolve error (root commit, merge without a
mainline, no test changes, unmerged PR, GitHub API error).

## Foundations (implemented)

Everything the pipeline stages build on is implemented and tested:

| Module | What it provides |
|---|---|
| `reporewind.models` | Frozen pydantic v2 models: `RepoRef` (parsed from `owner/repo`, HTTPS, `ssh://` and `git@host:` forms; credentials, extra path segments and query strings are rejected), `CommitInfo`, `FilePatch` (paths cannot escape the repo or touch `.git`), `ResolvedFix` (base must be a parent of the fix; source and test patches both present and disjoint) |
| `reporewind.errors` | One `RepoRewindError` hierarchy with a stable exit code per category (config, missing tool, command/git failure, and one per pipeline stage) |
| `reporewind.proc` | `CommandRunner` protocol plus a subprocess runner: argument lists only, empty stdin, typed missing-tool and timeout errors |
| `reporewind.gitops` | A no-shell `Git` wrapper: init, clone, fetch one commit by SHA, rev-parse, parents (read from the raw commit, so shallow boundaries keep them), commit dates, re-applicable diffs (binary and non-UTF-8 safe), read a file or list the tree at a revision, `apply --check` / `apply` / reverse, patch application to a scratch index returning a tree id, detached worktree checkout, work-tree reset |
| `reporewind.testing` | `RepoFactory`, exposed to pytest as the `repo_factory` fixture: throwaway repos with a fixed identity and clock, so fixture SHAs are identical on every machine; `make_bugfix` builds a pinned bug-fix repository (source fix plus a new test) for the later stages |

The git layer is **hermetic**: it ignores the user's global and system git
config, scrubs inherited `GIT_DIR`-style and `GIT_CONFIG_*` variables, stops
repository discovery at the target directory (`GIT_CEILING_DIRECTORIES`),
allows only file/git/http(s)/ssh transports and rejects option-like revisions,
remotes and URLs before git sees them. A diff produced on a laptop and one
produced in CI are byte-identical.

Real output of a short script (reference parsing, then a fix diff applied to
its parent in a throwaway repo):

```python
from reporewind.models import RepoRef
from reporewind.testing import RepoFactory

RepoRef.parse("git@github.com:Owner/Repo.git").recipe_stem  # 'owner__repo'

repo = RepoFactory(tmp).create(files={"calc.py": "def add(a, b):\n    return a - b\n"})
base = repo.head
fix = repo.commit("fix: add really adds", {"calc.py": "def add(a, b):\n    return a + b\n"})
patch = repo.git.diff(base, fix)
with repo.git.worktree(base, tmp / "work") as wt:
    wt.apply_check(patch)
    wt.apply(patch)
    wt.changed_paths()  # ('calc.py',)
```

```text
pallets/click                            -> pallets/click   https://github.com/pallets/click.git     pallets__click
https://github.com/psf/requests.git      -> psf/requests    https://github.com/psf/requests.git      psf__requests
git@github.com:Owner/Repo.git            -> Owner/Repo      https://github.com/Owner/Repo.git        owner__repo
fix: add really adds | parent is base: True | 2021-03-04T06:06:07+00:00
changed after apply: ('calc.py',)
```

## Quickstart (development)

```bash
git clone https://github.com/vipul21435/reporewind && cd reporewind
make install      # uv sync --locked + pre-commit hooks
make lint typecheck
make cov          # offline test suite with branch coverage
uv run reporewind resolve pallets/markupsafe --pr 477
```

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for
you) and git. The last command needs network access to GitHub; everything
before it runs offline. Docker is only needed for the build/verify stages
and the Docker end-to-end tests.

## Development

- Python 3.12, `src/` layout, dependencies locked in `uv.lock`
- `ruff` for lint and formatting, `mypy --strict` on `src/`
- `pytest` + `pytest-cov`; the default suite is fully offline. Tests that need
  the network or a Docker daemon are marked `e2e` and run separately. Anything
  that touches git builds its own throwaway repository with `repo_factory`.
  Current numbers, from `make cov` on 2026-09-29: 293 tests passed, 99.87%
  branch-inclusive coverage (gate: 85%). `make e2e` (live GitHub API and
  git remotes) ran 2 tests, both passed.
- GitHub API responses used by the offline tests are recorded from the live
  API and trimmed by `tests/fixtures/github/record.sh`.
- CI (GitHub Actions) runs lint, typecheck and the coverage suite on every push
  and pull request.

## License

MIT, see [LICENSE](LICENSE).
