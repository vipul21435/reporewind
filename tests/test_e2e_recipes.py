"""The committed recipes under ``recipes/`` are reproducible from the live repositories.

Excluded from the default suite; run with `make e2e` (network needed).
"""

from pathlib import Path

import pytest

from reporewind.recipes import DetectedBlock, RecipeStore, detect_at, load_recipe_file
from reporewind.resolve import RepoCache

pytestmark = pytest.mark.e2e

RECIPES = Path(__file__).resolve().parent.parent / "recipes"


@pytest.mark.parametrize("path", RecipeStore(RECIPES).paths(), ids=lambda p: p.stem)
def test_committed_recipe_is_what_detection_finds(tmp_path: Path, path: Path) -> None:
    stored = load_recipe_file(path)
    commit = stored.detected.commit
    assert commit is not None
    git = RepoCache(tmp_path).ensure_commit(stored.repo, commit)
    assert DetectedBlock.from_detection(detect_at(git, commit)) == stored.detected
