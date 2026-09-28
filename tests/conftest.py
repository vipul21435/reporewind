"""Shared fixtures. Everything here works offline."""

from pathlib import Path

import pytest

from reporewind.testing import RepoFactory


@pytest.fixture
def repo_factory(tmp_path: Path) -> RepoFactory:
    """Build deterministic throwaway git repositories under this test's tmp dir."""
    return RepoFactory(tmp_path / "repos")
