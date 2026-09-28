"""Resolve a fix commit to its base and split its diff into source and test patches."""

from reporewind.resolve.cache import RepoCache, home_dir
from reporewind.resolve.github import GitHubClient, MergeShape, PullRequestFix
from reporewind.resolve.patches import parse_patch
from reporewind.resolve.resolver import (
    Resolution,
    SplitProof,
    prove_split,
    resolve_fix,
    select_base,
)
from reporewind.resolve.serialize import dump_resolved_fix, load_resolved_fix
from reporewind.resolve.split import DiffSplit, SplitRules, split_patches

__all__ = [
    "DiffSplit",
    "GitHubClient",
    "MergeShape",
    "PullRequestFix",
    "RepoCache",
    "Resolution",
    "SplitProof",
    "SplitRules",
    "dump_resolved_fix",
    "home_dir",
    "load_resolved_fix",
    "parse_patch",
    "prove_split",
    "resolve_fix",
    "select_base",
    "split_patches",
]
