"""RepoRewind: rebuild a repository at a historical commit and prove a fail-to-pass flip."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("reporewind")
except PackageNotFoundError:  # pragma: no cover - raw checkout only
    __version__ = "0.0.0"

__all__ = ["__version__"]
