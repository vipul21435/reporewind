# RepoRewind

Rebuild an open-source Python repository at any historical commit inside a
digest-pinned Docker image, then prove that a fix commit flips its tests from
failing to passing.

> Status: early. The scaffold (packaging, lint, strict typing, tests, CI) is in
> place; the pipeline stages below are being built slice by slice, tracked in
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
| Resolve | Repo URL + fix SHA (or PR number) -> parent commit; split the diff into source vs test patches | planned |
| Recipe | Detect how to install and test the repo (pyproject, setup.py, setup.cfg, requirements, tox); YAML recipes with human overrides | planned |
| Pin | Resolve dependencies as of the commit date (`uv pip compile --exclude-newer`), infer the Python version, pin the base image by digest | planned |
| Build | Generate a Dockerfile, build it once per recipe hash behind a build lock | planned |
| Verify | Run tests on parent+test patch (must fail) and fix+test patch (must pass); parse JUnit XML; FAIL_TO_PASS / PASS_TO_PASS; flaky detection | planned |
| Export | Schema-validated task bundle: `task.json`, Dockerfile, patches, test lists | planned |

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
  the network or a Docker daemon are marked `e2e` and run separately.
- CI (GitHub Actions) runs lint, typecheck and the coverage suite on every push
  and pull request.

## License

MIT, see [LICENSE](LICENSE).
