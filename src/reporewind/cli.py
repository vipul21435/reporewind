"""Command-line entry point for RepoRewind.

Subcommands are registered here as each pipeline stage lands (resolve, recipe,
pin, build, verify, export). The root callback only handles global options.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from reporewind import __version__
from reporewind.errors import ConfigError, RepoRewindError
from reporewind.gitops import Git
from reporewind.models import RepoRef
from reporewind.resolve import (
    RepoCache,
    Resolution,
    SplitRules,
    dump_resolved_fix,
    home_dir,
    resolve_fix,
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
        str,
        typer.Argument(help="Fix commit: a full SHA, or any revision with --repo-dir."),
    ],
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
        if repo_dir is not None:
            git = Git(repo_dir)
            if not git.is_repo():
                raise ConfigError(f"--repo-dir {repo_dir} is not a git repository")
        else:
            cache = RepoCache(cache_dir if cache_dir is not None else home_dir())
            git = cache.ensure_commit(ref, fix, source=source)
        resolution = resolve_fix(git, ref, fix, mainline=mainline, rules=rules)
        text = dump_resolved_fix(resolution.fix)
        if output is None:
            typer.echo(text, nl=False)
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="ascii")
        if not quiet:
            typer.echo(_summary(resolution), err=True)
            if output is not None:
                typer.echo(f"  wrote   {output}", err=True)
