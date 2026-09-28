"""Build recipes: schema, detection from packaging metadata and the YAML store."""

from reporewind.recipes.detect import Detection, detect_at, detect_recipe
from reporewind.recipes.model import (
    DEFAULT_TEST_COMMANDS,
    RECIPE_SCHEMA_VERSION,
    Framework,
    InstallMode,
    Recipe,
    canonical_json,
    merge_patch,
    recipe_hash,
)
from reporewind.recipes.source import GitTreeSource, MemoryTreeSource, TreeSource

__all__ = [
    "DEFAULT_TEST_COMMANDS",
    "RECIPE_SCHEMA_VERSION",
    "Detection",
    "Framework",
    "GitTreeSource",
    "InstallMode",
    "MemoryTreeSource",
    "Recipe",
    "TreeSource",
    "canonical_json",
    "detect_at",
    "detect_recipe",
    "merge_patch",
    "recipe_hash",
]
