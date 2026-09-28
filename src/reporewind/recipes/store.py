"""YAML recipe files: detected values plus human overrides, one file per repository.

A recipe file lives at ``<recipes dir>/<owner>__<repo>.yaml`` and has two
layers::

    schema_version: 1
    repo: pallets/markupsafe
    detected:            # rewritten by `reporewind recipe detect`
      commit: <sha>      # the commit the values were read at
      backend: setuptools
      sources: [...]     # files that were read
      notes: [...]       # what detection skipped or assumed
      recipe: {...}      # sparse: only fields that had evidence
    overrides: {...}     # hand-written; kept across re-detection

The effective recipe is ``overrides`` applied to ``detected.recipe`` as a
JSON Merge Patch (RFC 7396) and then validated as a :class:`Recipe`. Files
are parsed with a safe loader that rejects duplicate keys (YAML would
otherwise keep the last one silently) and written atomically.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from reporewind.errors import RecipeError
from reporewind.models import RepoRef, Sha
from reporewind.recipes.detect import Detection
from reporewind.recipes.model import Recipe, merge_patch, recipe_hash

DEFAULT_RECIPES_DIR = Path("recipes")

HEADER = """\
# RepoRewind build recipe.
# `detected` is rewritten by `reporewind recipe detect`; put changes in
# `overrides`, a JSON Merge Patch (RFC 7396) over `detected.recipe`:
# mappings merge, lists and scalars replace, null removes a detected value.
"""


class DetectedBlock(BaseModel):
    """The ``detected`` layer of a recipe file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commit: Sha | None = None
    backend: str | None = None
    sources: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    recipe: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_detection(cls, detection: Detection) -> DetectedBlock:
        return cls(
            commit=detection.commit,
            backend=detection.backend,
            sources=detection.sources,
            notes=detection.notes,
            recipe=dict(detection.fields),
        )


def _format_errors(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        parts.append(f"{location}: {error['msg']}" if location else str(error["msg"]))
    return "; ".join(parts)


class RecipeFile(BaseModel):
    """One ``recipes/<owner>__<repo>.yaml`` file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    repo: RepoRef
    detected: DetectedBlock = Field(default_factory=DetectedBlock)
    overrides: dict[str, Any] = Field(default_factory=dict)

    def recipe(self) -> Recipe:
        """The effective recipe; raises :class:`RecipeError` if it is invalid."""
        detected = dict(self.detected.recipe)
        if "test_framework" in self.overrides and "test_command" not in self.overrides:
            # A detected command belongs to the detected runner; a framework
            # override brings that framework's default command instead.
            detected.pop("test_command", None)
        merged = merge_patch(detected, self.overrides)
        try:
            return Recipe.model_validate(merged)
        except ValidationError as exc:
            raise RecipeError(
                f"recipe for {self.repo} (detected + overrides) is invalid: {_format_errors(exc)}"
            ) from None

    def recipe_hash(self) -> str:
        return recipe_hash(self.recipe())

    @property
    def overridden(self) -> tuple[str, ...]:
        """Top-level recipe fields that the overrides touch, sorted."""
        return tuple(sorted(self.overrides))

    def with_detection(self, detection: Detection) -> RecipeFile:
        """A copy with a fresh ``detected`` layer and the same overrides."""
        return RecipeFile(
            repo=self.repo,
            detected=DetectedBlock.from_detection(detection),
            overrides=self.overrides,
        )

    def to_data(self) -> dict[str, Any]:
        """Plain data in file order (``repo`` as a string, ``None`` fields dropped)."""
        return {
            "schema_version": self.schema_version,
            "repo": str(self.repo),
            "detected": self.detected.model_dump(mode="json", exclude_none=True),
            "overrides": self.overrides,
        }


class _StrictLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that rejects duplicate mapping keys."""


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    seen: set[object] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            duplicate = key in seen
            seen.add(key)
        except TypeError:
            continue  # unhashable key: construct_mapping reports it
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
    return loader.construct_mapping(node, deep=True)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def parse_recipe_file(text: str, *, origin: str = "<recipe>") -> RecipeFile:
    """Parse and fully validate recipe YAML; raise :class:`RecipeError` if invalid."""
    try:
        data = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise RecipeError(f"{origin}: not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise RecipeError(f"{origin}: expected a mapping at the top level")
    try:
        recipe_file = RecipeFile.model_validate(data)
    except ValidationError as exc:
        raise RecipeError(f"{origin}: {_format_errors(exc)}") from None
    try:
        recipe_file.recipe()
    except RecipeError as exc:
        raise RecipeError(f"{origin}: {exc}") from None
    return recipe_file


def dump_recipe_file(recipe_file: RecipeFile) -> str:
    """The YAML text of a recipe file (header comment included); validates first."""
    recipe_file.recipe()
    body = yaml.safe_dump(
        recipe_file.to_data(),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=False,
        width=100,
    )
    return HEADER + body


def _same_repo(a: RepoRef, b: RepoRef) -> bool:
    """GitHub-style hosts treat owner and name case-blind, like ``recipe_stem``."""
    return (a.host.lower(), a.owner.lower(), a.name.lower()) == (
        b.host.lower(),
        b.owner.lower(),
        b.name.lower(),
    )


def load_recipe_file(path: Path, *, expected: RepoRef | None = None) -> RecipeFile:
    """Read, validate and check the name of the recipe file at ``path``."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RecipeError(f"cannot read recipe file {path}: {exc}") from None
    recipe_file = parse_recipe_file(text, origin=str(path))
    stem = recipe_file.repo.recipe_stem
    if path.name != f"{stem}.yaml":
        raise RecipeError(f"{path}: the recipe for {recipe_file.repo} must be named {stem}.yaml")
    if expected is not None and not _same_repo(recipe_file.repo, expected):
        raise RecipeError(f"{path} is the recipe for {recipe_file.repo}, not {expected}")
    return recipe_file


class RecipeStore:
    """The recipe files under one directory (``recipes/`` by default)."""

    def __init__(self, root: Path | str = DEFAULT_RECIPES_DIR) -> None:
        self.root = Path(root)

    def path_for(self, repo: RepoRef) -> Path:
        return self.root / f"{repo.recipe_stem}.yaml"

    def paths(self) -> tuple[Path, ...]:
        """Every ``*.yaml`` file in the store, sorted."""
        if not self.root.is_dir():
            return ()
        return tuple(sorted(self.root.glob("*.yaml")))

    def load(self, repo: RepoRef) -> RecipeFile:
        path = self.path_for(repo)
        if not path.is_file():
            raise RecipeError(
                f"no recipe for {repo} at {path}; run `reporewind recipe detect` first"
            )
        return load_recipe_file(path, expected=repo)

    def merge_detection(self, repo: RepoRef, detection: Detection) -> RecipeFile:
        """A recipe file with a fresh detected layer and any existing overrides kept."""
        if self.path_for(repo).is_file():
            recipe_file = self.load(repo).with_detection(detection)
        else:
            recipe_file = RecipeFile(repo=repo, detected=DetectedBlock.from_detection(detection))
        recipe_file.recipe()
        return recipe_file

    def save(self, recipe_file: RecipeFile) -> Path:
        """Write ``recipe_file`` atomically; return its path."""
        text = dump_recipe_file(recipe_file)
        path = self.path_for(recipe_file.repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            Path(tmp).chmod(0o644)  # mkstemp creates 0600; recipes are shared files
            Path(tmp).replace(path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path


__all__ = [
    "DEFAULT_RECIPES_DIR",
    "HEADER",
    "DetectedBlock",
    "RecipeFile",
    "RecipeStore",
    "dump_recipe_file",
    "load_recipe_file",
    "parse_recipe_file",
]
