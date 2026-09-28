# RepoRewind

Rebuild an open-source Python repository at any historical commit inside a
digest-pinned Docker image, then prove that a fix commit flips its tests from
failing to passing.

> Status: early. The scaffold (packaging, lint, strict typing, tests, CI) and
> the foundations (domain models, typed errors, a hermetic git layer and an
> offline repository fixture) are in place; the pipeline stages below are being
> built slice by slice, tracked in [PLAN.md](PLAN.md). Every stage is marked
> with its current state so nothing here claims more than the code does.

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
| Resolve | Repo URL + fix SHA (or PR number) -> parent commit; split the diff into source vs test patches | planned |
| Recipe | Detect how to install and test the repo (pyproject, setup.py, setup.cfg, requirements, tox); YAML recipes with human overrides | planned |
| Pin | Resolve dependencies as of the commit date (`uv pip compile --exclude-newer`), infer the Python version, pin the base image by digest | planned |
| Build | Generate a Dockerfile, build it once per recipe hash behind a build lock | planned |
| Verify | Run tests on parent+test patch (must fail) and fix+test patch (must pass); parse JUnit XML; FAIL_TO_PASS / PASS_TO_PASS; flaky detection | planned |
| Export | Schema-validated task bundle: `task.json`, Dockerfile, patches, test lists | planned |

## Foundations (implemented)

Everything the pipeline stages build on is implemented and tested:

| Module | What it provides |
|---|---|
| `reporewind.models` | Frozen pydantic v2 models: `RepoRef` (parsed from `owner/repo`, HTTPS, `ssh://` and `git@host:` forms; credentials, extra path segments and query strings are rejected), `CommitInfo`, `FilePatch` (paths cannot escape the repo or touch `.git`), `ResolvedFix` (base must be a parent of the fix; source and test patches both present and disjoint) |
| `reporewind.errors` | One `RepoRewindError` hierarchy with a stable exit code per category (config, missing tool, command/git failure, and one per pipeline stage) |
| `reporewind.proc` | `CommandRunner` protocol plus a subprocess runner: argument lists only, empty stdin, typed missing-tool and timeout errors |
| `reporewind.gitops` | A no-shell `Git` wrapper: init, clone, fetch one commit by SHA, rev-parse, parents, commit dates, re-applicable diffs (binary and non-UTF-8 safe), read a file or list the tree at a revision, `apply --check` / `apply` / reverse, detached worktree checkout, work-tree reset |
| `reporewind.testing` | `RepoFactory`, exposed to pytest as the `repo_factory` fixture: throwaway repos with a fixed identity and clock, so fixture SHAs are identical on every machine |

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
uv run reporewind --help
```

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for
you) and git. Docker is only needed for the build/verify stages and the
end-to-end tests (`make e2e`).

## Development

- Python 3.12, `src/` layout, dependencies locked in `uv.lock`
- `ruff` for lint and formatting, `mypy --strict` on `src/`
- `pytest` + `pytest-cov`; the default suite is fully offline. Tests that need
  the network or a Docker daemon are marked `e2e` and run separately. Anything
  that touches git builds its own throwaway repository with `repo_factory`.
  Current numbers, from `make cov` on 2026-09-29: 148 tests passed, 99.60%
  branch-inclusive coverage (gate: 85%).
- CI (GitHub Actions) runs lint, typecheck and the coverage suite on every push
  and pull request.

## License

MIT, see [LICENSE](LICENSE).
