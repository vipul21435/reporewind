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
from reporewind.recipes.store import (
    DEFAULT_RECIPES_DIR,
    DetectedBlock,
    RecipeFile,
    RecipeStore,
    dump_recipe_file,
    load_recipe_file,
    parse_recipe_file,
)

__all__ = [
    "DEFAULT_RECIPES_DIR",
    "DEFAULT_TEST_COMMANDS",
    "RECIPE_SCHEMA_VERSION",
    "DetectedBlock",
    "Detection",
    "Framework",
    "GitTreeSource",
    "InstallMode",
    "MemoryTreeSource",
    "Recipe",
    "RecipeFile",
    "RecipeStore",
    "TreeSource",
    "canonical_json",
    "detect_at",
    "detect_recipe",
    "dump_recipe_file",
    "load_recipe_file",
    "merge_patch",
    "parse_recipe_file",
    "recipe_hash",
]
