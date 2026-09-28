"""Command-line entry point for RepoRewind.

Subcommands are registered here as each pipeline stage lands (resolve, recipe,
pin, build, verify, export). The root callback only handles global options.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from reporewind import __version__
from reporewind.build import (
    BuildCache,
    DockerBuilder,
    ensure_image,
    environment_key,
    export_source,
    image_tag,
    render_dockerfile,
)
from reporewind.errors import BuildError, ConfigError, RecipeError, RepoRewindError
from reporewind.gitops import Git
from reporewind.models import RepoRef
from reporewind.pinning import (
    DEFAULT_PLATFORM,
    PIN_FILE,
    PinOptions,
    PinResult,
    dump_pin_result,
    format_timestamp,
    is_offline,
    load_pin_result,
    pin_commit,
    write_pin,
)
from reporewind.proc import CommandRunner, SubprocessRunner
from reporewind.recipes import (
    DEFAULT_RECIPES_DIR,
    GitTreeSource,
    Recipe,
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


def command_runner() -> CommandRunner:
    """The runner ``pin`` uses for uv and docker (replaced in tests)."""
    return SubprocessRunner()


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


def _parse_cutoff(value: str) -> datetime:
    """An ISO 8601 date or date-time; a value without a timezone is taken as UTC."""
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        message = f"--exclude-newer {value!r} is not an ISO 8601 date or date-time"
        raise ConfigError(message) from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _pin_recipe(
    store: RecipeStore, ref: RepoRef, git: Git, target: str
) -> tuple[Recipe, tuple[str, ...]]:
    """The stored recipe for ``ref``, or one detected at ``target`` if there is no file."""
    if store.path_for(ref).is_file():
        recipe_file = store.load(ref)
        detected_at = recipe_file.detected.commit
        notes: tuple[str, ...] = ()
        if detected_at is not None and detected_at != target:
            notes = (f"the recipe was detected at {detected_at[:12]}, not at {target[:12]}",)
        return recipe_file.recipe(), notes
    detection = detect_at(git, target)
    note = (
        f"no recipe file at {store.path_for(ref)}; used the recipe detected at {target[:12]}"
        " (run `reporewind recipe detect` to review and store it)"
    )
    return detection.recipe(), (note,)


def _state_dir(cache_dir: Path | None) -> Path:
    return cache_dir if cache_dir is not None else home_dir()


def _default_pin_dir(ref: RepoRef, target: str, cache_dir: Path | None) -> Path:
    return _state_dir(cache_dir) / "pins" / ref.host / ref.recipe_stem / target


def _pin_summary(result: PinResult, where: str, lock_path: Path, pin_path: Path) -> str:
    python, image = result.python, result.base_image
    lines = [
        f"pinned {result.repo} at {where}, committed {format_timestamp(result.commit_date)}",
        f"  python   {python.version} ({python.reason})",
        f"  image    {image.reference} ({image.source})",
        f"  lock     {_plural(len(result.packages), 'package')} published up to"
        f" {format_timestamp(result.exclude_newer)}, {result.platform}",
        f"  recipe   {result.recipe_hash}",
    ]
    lines += [f"  note     {note}" for note in result.notes]
    lines += [f"  wrote    {lock_path}", f"  wrote    {pin_path}"]
    return "\n".join(lines)


@app.command("pin")
def pin_cmd(
    repo: Annotated[str, typer.Argument(help="Repository: owner/repo or a clone URL.")],
    rev: Annotated[
        str,
        typer.Argument(help="Fix commit; its parent is pinned, the commit that gets built."),
    ],
    at_rev: Annotated[
        bool, typer.Option("--at-rev", help="Pin REV itself, not its parent.")
    ] = False,
    mainline: Annotated[
        int | None,
        typer.Option("--mainline", "-m", min=1, help="For a merge commit, the parent to pin."),
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
        typer.Option(help="State directory for cached repos and pins [default: $REPOREWIND_HOME]."),
    ] = None,
    recipes_dir: RecipesDirOption = DEFAULT_RECIPES_DIR,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            "-o",
            file_okay=False,
            help="Where to write requirements.lock and pin.json"
            " [default: $REPOREWIND_HOME/pins/<host>/<owner>__<repo>/<sha>].",
        ),
    ] = None,
    python: Annotated[
        str | None,
        typer.Option("--python", help="Use this Python version (X.Y) instead of inferring it."),
    ] = None,
    exclude_newer: Annotated[
        str | None,
        typer.Option(
            "--exclude-newer",
            help="Lock packages published up to this ISO 8601 date [default: the commit date].",
        ),
    ] = None,
    platform: Annotated[
        str, typer.Option(help="Target platform passed to uv --python-platform.")
    ] = DEFAULT_PLATFORM,
    hashes: Annotated[
        bool, typer.Option("--hashes/--no-hashes", help="Record sha256 hashes in the lock.")
    ] = True,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline",
            help="Use recorded image digests and uv --offline (also $REPOREWIND_OFFLINE=1).",
        ),
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Do not print the summary on stderr.")
    ] = False,
) -> None:
    """Pin the Python version, dependencies and base image of the commit that gets built."""
    with _exit_on_error():
        ref = RepoRef.parse(repo)
        if at_rev and mainline is not None:
            raise ConfigError("--mainline picks a parent; it cannot be used with --at-rev")
        options = PinOptions(
            python=python,
            exclude_newer=_parse_cutoff(exclude_newer) if exclude_newer is not None else None,
            platform=platform,
            generate_hashes=hashes,
            offline=offline or is_offline(),
        )
        git = _open_repository(ref, rev, repo_dir=repo_dir, source=source, cache_dir=cache_dir)
        commit = git.commit_info(rev)
        target = commit.sha if at_rev else select_base(commit, mainline)
        recipe, notes = _pin_recipe(RecipeStore(recipes_dir), ref, git, target)
        result, lock_text = pin_commit(
            GitTreeSource(git, target),
            ref,
            git.commit_info(target),
            recipe,
            command_runner(),
            options,
            notes=notes,
        )
        if output_dir is None:
            output_dir = _default_pin_dir(ref, target, cache_dir)
        lock_path, pin_path = write_pin(result, lock_text, output_dir)
        typer.echo(dump_pin_result(result), nl=False)
        if quiet:
            return
        where = target[:12]
        if not at_rev:
            number = commit.parents.index(target) + 1
            where += f" (parent {number} of {len(commit.parents)} of {commit.sha[:12]})"
        typer.echo(_pin_summary(result, where, lock_path, pin_path), err=True)


@app.command("build")
def build_cmd(
    repo: Annotated[str, typer.Argument(help="Repository: owner/repo or a clone URL.")],
    rev: Annotated[
        str,
        typer.Argument(help="Fix commit; its parent is built, as `reporewind pin` pinned it."),
    ],
    at_rev: Annotated[
        bool, typer.Option("--at-rev", help="Build REV itself, not its parent.")
    ] = False,
    mainline: Annotated[
        int | None,
        typer.Option("--mainline", "-m", min=1, help="For a merge commit, the parent to build."),
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
        typer.Option(
            help="State directory for repos, pins and the build cache [default: $REPOREWIND_HOME]."
        ),
    ] = None,
    recipes_dir: RecipesDirOption = DEFAULT_RECIPES_DIR,
    pin_dir: Annotated[
        Path | None,
        typer.Option(
            "--pin-dir",
            file_okay=False,
            help="Directory with pin.json and requirements.lock"
            " [default: $REPOREWIND_HOME/pins/<host>/<owner>__<repo>/<sha>].",
        ),
    ] = None,
    lock_timeout: Annotated[
        float,
        typer.Option(min=0, help="Seconds to wait for another process building the same image."),
    ] = 1800.0,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the Dockerfile and image tag; do not call docker."),
    ] = False,
) -> None:
    """Render the Dockerfile for the pinned commit and build its image once."""
    with _exit_on_error():
        ref = RepoRef.parse(repo)
        if at_rev and mainline is not None:
            raise ConfigError("--mainline picks a parent; it cannot be used with --at-rev")
        git = _open_repository(ref, rev, repo_dir=repo_dir, source=source, cache_dir=cache_dir)
        commit = git.commit_info(rev)
        target = commit.sha if at_rev else select_base(commit, mainline)
        pins = pin_dir if pin_dir is not None else _default_pin_dir(ref, target, cache_dir)
        if not (pins / PIN_FILE).is_file():
            raise BuildError(f"no {PIN_FILE} in {pins}; run `reporewind pin` first")
        pin = load_pin_result(pins / PIN_FILE)
        if pin.commit != target:
            raise BuildError(f"{pins / PIN_FILE} pins {pin.commit[:12]}, not {target[:12]}")
        try:
            lock_text = (pins / pin.lock_file).read_text("utf-8", "surrogateescape")
        except OSError as exc:
            raise BuildError(f"cannot read the lock file: {exc}") from None
        recipe, _ = _pin_recipe(RecipeStore(recipes_dir), ref, git, target)
        dockerfile = render_dockerfile(recipe, pin, lock_text)
        cache = BuildCache(_state_dir(cache_dir) / "build", lock_timeout=lock_timeout)
        key = environment_key(pin, lock_text)
        if dry_run:
            typer.echo(dockerfile, nl=False)
            entry = cache.get(key)
            state = f"indexed as {entry.image_id}" if entry else "not built yet"
            lines = [
                f"dry run: {ref} at {target[:12]}",
                f"  key      {key}",
                f"  tag      {image_tag(ref, key)} ({state})",
                f"  context  Dockerfile, {pin.lock_file}, src/ (git archive of {target[:12]})",
                f"  run      docker run --rm --network none {image_tag(ref, key)}",
            ]
            typer.echo("\n".join(lines), err=True)
            return
        outcome = ensure_image(
            pin,
            dockerfile,
            lock_text,
            builder=DockerBuilder(command_runner()),
            cache=cache,
            write_source=lambda dest: export_source(git, target, dest),
        )
        payload = {
            "key": outcome.key,
            "tag": outcome.tag,
            "image_id": outcome.image_id,
            "status": outcome.status,
        }
        typer.echo(json.dumps(payload, indent=2))
