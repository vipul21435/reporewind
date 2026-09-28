"""Command-line entry point for RepoRewind.

Subcommands are registered here as each pipeline stage lands (resolve, recipe,
pin, build, verify, export). The root callback only handles global options.
"""

from __future__ import annotations

from typing import Annotated

import typer

from reporewind import __version__

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
