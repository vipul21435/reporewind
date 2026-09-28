"""Shared fixtures. Everything here works offline."""

from pathlib import Path

import pytest

from reporewind.testing import BugfixRepo, RepoFactory, make_bugfix


@pytest.fixture
def repo_factory(tmp_path: Path) -> RepoFactory:
    """Build deterministic throwaway git repositories under this test's tmp dir."""
    return RepoFactory(tmp_path / "repos")


@pytest.fixture
def bugfix(repo_factory: RepoFactory) -> BugfixRepo:
    """root -> base -> fix, where the fix corrects src/calc/core.py and adds a test."""
    return make_bugfix(repo_factory)
