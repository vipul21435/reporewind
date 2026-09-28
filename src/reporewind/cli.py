"""Command-line entry point for RepoRewind.

Subcommands are registered here as each pipeline stage lands (resolve, recipe,
pin, build, verify, export). The root callback only handles global options.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from reporewind import __version__
from reporewind.errors import ConfigError, RecipeError, RepoRewindError
from reporewind.gitops import Git
from reporewind.models import RepoRef
from reporewind.recipes import (
    DEFAULT_RECIPES_DIR,
    RecipeFile,
    RecipeStore,
    detect_at,
    dump_recipe_file,
    load_recipe_file,
)
from reporewind.resolve import (
    GitHubClient,
    PullRequestFix,
    RepoCache,
    Resolution,
    SplitRules,
    dump_resolved_fix,
    home_dir,
    resolve_fix,
    select_base,
)

app = typer.Typer(
    name="reporewind",
    help="Rebuild a Python repo at a historical commit and prove a fail-to-pass flip.",
    no_args_is_help=True,
    add_completion=False,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"reporewind {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Show the installed version and exit.",
        ),
    ] = False,
) -> None:
    """RepoRewind command-line interface."""


@app.command("version")
def version_cmd() -> None:
    """Print the installed RepoRewind version."""
    typer.echo(__version__)


def github_client(repo: RepoRef) -> GitHubClient:
    """The GitHub client used by ``resolve --pr`` (replaced in tests)."""
    return GitHubClient.for_repo(repo)


@contextmanager
def _exit_on_error() -> Iterator[None]:
    """Report a RepoRewind error on stderr and exit with its category's code."""
    try:
        yield
    except RepoRewindError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from exc


def _split_rules(
    test_dirs: list[str] | None,
    test_files: list[str] | None,
    include: list[str] | None,
    exclude: list[str] | None,
) -> SplitRules:
    overrides: dict[str, tuple[str, ...]] = {}
    for key, values in (
        ("test_dirs", test_dirs),
        ("test_files", test_files),
        ("include", include),
        ("exclude", exclude),
    ):
        if values:
            overrides[key] = tuple(values)
    try:
        return SplitRules.model_validate(overrides)
    except ValidationError as exc:
        detail = "; ".join(str(err["msg"]) for err in exc.errors())
        raise ConfigError(f"invalid split rules: {detail}") from None


def _open_repository(
    ref: RepoRef,
    rev: str,
    *,
    repo_dir: Path | None,
    source: str | None,
    cache_dir: Path | None,
) -> Git:
    """A local clone given with --repo-dir, or the cache with ``rev`` fetched into it."""
    if repo_dir is not None:
        git = Git(repo_dir)
        if not git.is_repo():
            raise ConfigError(f"--repo-dir {repo_dir} is not a git repository")
        return git
    cache = RepoCache(cache_dir if cache_dir is not None else home_dir())
    return cache.ensure_commit(ref, rev, source=source)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _summary(resolution: Resolution) -> str:
    fix, proof = resolution.fix, resolution.proof
    parent_no = fix.fix.parents.index(fix.base.sha) + 1
    source = ", ".join(p.path for p in fix.source_patches)
    tests = ", ".join(p.path for p in fix.test_patches)
    return "\n".join(
        [
            f"resolved {fix.repo} fix {fix.fix.sha[:12]} ({fix.fix.subject})",
            f"  base    {fix.base.sha[:12]} (parent {parent_no} of {len(fix.fix.parents)})",
            f"  source  {_plural(len(fix.source_patches), 'file')}: {source}",
            f"  tests   {_plural(len(fix.test_patches), 'file')}: {tests}",
            "  proof   source and test patches each apply cleanly to base; base + both"
            f" == fix tree {proof.fix_tree[:12]}",
        ]
    )


@app.command("resolve")
def resolve_cmd(
    repo: Annotated[str, typer.Argument(help="Repository: owner/repo or a clone URL.")],
    fix: Annotated[
        str | None,
        typer.Argument(help="Fix commit: a full SHA, or any revision with --repo-dir."),
    ] = None,
    pr: Annotated[
        int | None,
        typer.Option(
            "--pr",
            min=1,
            help="Resolve a merged GitHub pull request instead of a SHA ($GITHUB_TOKEN optional).",
        ),
    ] = None,
    mainline: Annotated[
        int | None,
        typer.Option(
            "--mainline",
            "-m",
            min=1,
            help="For a merge commit, the 1-based parent to use as base (1 = merged into).",
        ),
    ] = None,
    repo_dir: Annotated[
        Path | None,
        typer.Option(
            "--repo-dir",
            file_okay=False,
            help="Read commits from this existing local clone instead of the cache.",
        ),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option(help="Fetch from this URL or path instead of the repository's clone URL."),
    ] = None,
    cache_dir: Annotated[
        Path | None,
        typer.Option(help="State directory for cached repos [default: $REPOREWIND_HOME]."),
    ] = None,
    test_dir: Annotated[
        list[str] | None,
        typer.Option(help="Directory name marking test files (repeatable; replaces defaults)."),
    ] = None,
    test_file: Annotated[
        list[str] | None,
        typer.Option(help="Basename glob of test files (repeatable; replaces defaults)."),
    ] = None,
    include: Annotated[
        list[str] | None,
        typer.Option(help="Path glob that always counts as a test (repeatable)."),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option(help="Path glob that never counts as a test; wins over all (repeatable)."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write the ResolvedFix JSON here, not stdout."),
    ] = None,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Do not print the summary on stderr.")
    ] = False,
) -> None:
    """Resolve a fix commit to its base, split the diff and prove both halves apply."""
    with _exit_on_error():
        ref = RepoRef.parse(repo)
        rules = _split_rules(test_dir, test_file, include, exclude)
        pull: PullRequestFix | None = None
        if fix is not None and pr is not None:
            raise ConfigError("give either a fix commit or --pr NUMBER, not both")
        if pr is not None:
            if mainline is not None:
                raise ConfigError("--mainline is chosen from the pull request; drop it with --pr")
            with github_client(ref) as client:
                pull = client.resolve_pull_request(ref, pr)
            fix, mainline = pull.fix_sha, pull.mainline
        if fix is None:
            raise ConfigError("give a fix commit or --pr NUMBER")
        git = _open_repository(ref, fix, repo_dir=repo_dir, source=source, cache_dir=cache_dir)
        resolution = resolve_fix(git, ref, fix, mainline=mainline, rules=rules, pr_number=pr)
        text = dump_resolved_fix(resolution.fix)
        if output is None:
            typer.echo(text, nl=False)
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="ascii")
        if not quiet:
            if pull is not None:
                typer.echo(
                    f"pull request #{pull.number} ({pull.title}) landed as a {pull.shape.value}"
                    f" {pull.fix_sha[:12]}",
                    err=True,
                )
            typer.echo(_summary(resolution), err=True)
            if output is not None:
                typer.echo(f"  wrote   {output}", err=True)


recipe_app = typer.Typer(
    help="Detect, show and validate per-repository build recipes.",
    no_args_is_help=True,
)
app.add_typer(recipe_app, name="recipe")

RecipesDirOption = Annotated[
    Path,
    typer.Option(
        "--recipes-dir",
        file_okay=False,
        help="Directory holding <owner>__<repo>.yaml recipe files.",
    ),
]


@recipe_app.command("detect")
def recipe_detect_cmd(
    repo: Annotated[str, typer.Argument(help="Repository: owner/repo or a clone URL.")],
    rev: Annotated[
        str,
        typer.Argument(
            help="Fix commit; the recipe is read at its parent, the commit that gets built."
        ),
    ],
    at_rev: Annotated[
        bool, typer.Option("--at-rev", help="Read the recipe at REV itself, not at its parent.")
    ] = False,
    mainline: Annotated[
        int | None,
        typer.Option("--mainline", "-m", min=1, help="For a merge commit, the parent to read at."),
    ] = None,
    repo_dir: Annotated[
        Path | None,
        typer.Option(
            "--repo-dir",
            file_okay=False,
            help="Read commits from this existing local clone instead of the cache.",
        ),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option(help="Fetch from this URL or path instead of the repository's clone URL."),
    ] = None,
    cache_dir: Annotated[
        Path | None,
        typer.Option(help="State directory for cached repos [default: $REPOREWIND_HOME]."),
    ] = None,
    recipes_dir: RecipesDirOption = DEFAULT_RECIPES_DIR,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Print the recipe file instead of writing it."),
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Do not print the summary on stderr.")
    ] = False,
) -> None:
    """Detect a build recipe from packaging metadata and store it, keeping overrides."""
    with _exit_on_error():
        ref = RepoRef.parse(repo)
        if at_rev and mainline is not None:
            raise ConfigError("--mainline picks a parent; it cannot be used with --at-rev")
        git = _open_repository(ref, rev, repo_dir=repo_dir, source=source, cache_dir=cache_dir)
        commit = git.commit_info(rev)
        target = commit.sha if at_rev else select_base(commit, mainline)
        detection = detect_at(git, target)
        store = RecipeStore(recipes_dir)
        recipe_file = store.merge_detection(ref, detection)
        if dry_run:
            typer.echo(dump_recipe_file(recipe_file), nl=False)
        else:
            path = store.save(recipe_file)
        if quiet:
            return
        where = target[:12]
        if not at_rev:
            number = commit.parents.index(target) + 1
            where += f" (parent {number} of {len(commit.parents)} of {commit.sha[:12]})"
        lines = [f"detected {ref} recipe at {where}"]
        lines.append(f"  backend  {detection.backend or 'none'}")
        lines.append(f"  sources  {', '.join(detection.sources) or 'none'}")
        lines += [f"  note     {note}" for note in detection.notes]
        kept = recipe_file.overridden
        suffix = f" ({_plural(len(kept), 'override')} kept: {', '.join(kept)})" if kept else ""
        lines.append(f"  hash     {recipe_file.recipe_hash()}{suffix}")
        if not dry_run:
            lines.append(f"  wrote    {path}")
        typer.echo("\n".join(lines), err=True)


def _show_data(recipe_file: RecipeFile) -> dict[str, object]:
    recipe = recipe_file.recipe()
    return {
        "repo": str(recipe_file.repo),
        "recipe_hash": recipe_file.recipe_hash(),
        "detected_at": recipe_file.detected.commit,
        "overridden": list(recipe_file.overridden),
        "recipe": recipe.model_dump(mode="json"),
    }


@recipe_app.command("show")
def recipe_show_cmd(
    repo: Annotated[str, typer.Argument(help="Repository: owner/repo or a clone URL.")],
    recipes_dir: RecipesDirOption = DEFAULT_RECIPES_DIR,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of YAML.")] = False,
) -> None:
    """Print the effective recipe (detected values with overrides applied) and its hash."""
    with _exit_on_error():
        recipe_file = RecipeStore(recipes_dir).load(RepoRef.parse(repo))
        data = _show_data(recipe_file)
        if as_json:
            typer.echo(json.dumps(data, indent=2, ensure_ascii=True))
        else:
            typer.echo(yaml.safe_dump(data, sort_keys=False, default_flow_style=False), nl=False)


@recipe_app.command("validate")
def recipe_validate_cmd(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(help="Recipe files to check [default: every *.yaml in --recipes-dir]."),
    ] = None,
    recipes_dir: RecipesDirOption = DEFAULT_RECIPES_DIR,
) -> None:
    """Check recipe files: YAML, schema, file name and the merged recipe."""
    with _exit_on_error():
        targets = list(paths) if paths else list(RecipeStore(recipes_dir).paths())
        if not targets:
            raise ConfigError(f"no recipe files to validate under {recipes_dir}")
        failures = 0
        for path in targets:
            try:
                recipe_file = load_recipe_file(path)
            except RecipeError as exc:
                failures += 1
                typer.echo(f"invalid  {exc}", err=True)
                continue
            typer.echo(f"ok       {path}  {recipe_file.recipe_hash()}")
        if failures:
            raise RecipeError(f"{_plural(failures, 'recipe file')} failed validation")
