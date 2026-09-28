"""Resolve a fix commit to its base and prove the source/test split is sound.

Given a fix commit, :func:`resolve_fix`

1. picks the base: the only parent of an ordinary commit, or the parent named
   by ``mainline`` for a merge commit (root commits have nothing to rewind to
   and are rejected; merge commits without a mainline are ambiguous and are
   rejected too);
2. diffs base..fix and splits the diff per file into source and test patches
   with :class:`~reporewind.resolve.split.SplitRules`;
3. proves the split in a scratch index (:func:`prove_split`): the source patch
   and the test patch each apply cleanly to the base on their own, and base +
   source + test reproduces the fix commit's tree object id exactly, so
   nothing was lost or duplicated by the split.

Nothing in the repository's work tree, index or refs changes along the way.
"""

from __future__ import annotations

from dataclasses import dataclass

from reporewind.errors import PatchApplyError, ResolveError
from reporewind.gitops import Git
from reporewind.models import CommitInfo, RepoRef, ResolvedFix, join_patches
from reporewind.resolve.patches import parse_patch
from reporewind.resolve.split import DiffSplit, SplitRules, split_patches

SHORT = 12


@dataclass(frozen=True, slots=True)
class SplitProof:
    """Tree ids recorded while proving a split (all computed, none assumed)."""

    base_tree: str
    source_tree: str
    """Tree of base + source patch."""
    test_tree: str
    """Tree of base + test patch."""
    fix_tree: str
    """Tree of base + source + test, equal to the fix commit's tree."""


@dataclass(frozen=True, slots=True)
class Resolution:
    """A resolved fix together with the proof that its split is sound."""

    fix: ResolvedFix
    proof: SplitProof


def select_base(fix: CommitInfo, mainline: int | None = None) -> str:
    """Return the SHA of the parent that ``fix`` is measured against.

    ``mainline`` is 1-based, like ``git cherry-pick -m``: for a merge commit,
    1 is usually the branch that was merged into. It may be given for an
    ordinary commit too, where only 1 is valid.
    """
    short = fix.sha[:SHORT]
    count = len(fix.parents)
    if count == 0:
        raise ResolveError(f"fix {short} is a root commit: it has no parent to rewind to")
    if mainline is None:
        if count > 1:
            raise ResolveError(
                f"fix {short} is a merge commit with {count} parents; pass a mainline "
                f"(1-{count}) to choose the base, usually 1 for the branch merged into"
            )
        return fix.parents[0]
    if not 1 <= mainline <= count:
        raise ResolveError(
            f"mainline {mainline} is out of range: fix {short} has {count} parent(s)"
        )
    return fix.parents[mainline - 1]


def prove_split(git: Git, base: str, fix: str, split: DiffSplit) -> SplitProof:
    """Check the split in a scratch index; raise :class:`ResolveError` if it is unsound."""
    source_patch = join_patches(split.source)
    test_patch = join_patches(split.tests)
    trees: dict[str, str] = {}
    for name, patch in (("source", source_patch), ("test", test_patch)):
        try:
            trees[name] = git.apply_to_tree(base, patch)
        except PatchApplyError as exc:
            raise ResolveError(
                f"the {name} patch does not apply cleanly to base {base[:SHORT]}: {exc}"
            ) from exc
    try:
        combined = git.apply_to_tree(base, source_patch, test_patch)
    except PatchApplyError as exc:
        raise ResolveError(
            f"source and test patches do not apply together to base {base[:SHORT]}: {exc}"
        ) from exc
    fix_tree = git.tree_id(fix)
    if combined != fix_tree:
        raise ResolveError(
            f"base {base[:SHORT]} + source + test gives tree {combined[:SHORT]}, but fix "
            f"{fix[:SHORT]} has tree {fix_tree[:SHORT]}: the split lost or changed content"
        )
    return SplitProof(
        base_tree=git.tree_id(base),
        source_tree=trees["source"],
        test_tree=trees["test"],
        fix_tree=fix_tree,
    )


def resolve_fix(
    git: Git,
    repo: RepoRef,
    fix: str,
    *,
    mainline: int | None = None,
    rules: SplitRules | None = None,
    pr_number: int | None = None,
) -> Resolution:
    """Resolve ``fix`` (any revision ``git`` can see) into a proven :class:`ResolvedFix`."""
    fix_info = git.commit_info(fix)
    base_sha = select_base(fix_info, mainline)
    short, base_short = fix_info.sha[:SHORT], base_sha[:SHORT]
    if not git.has_commit(base_sha):
        raise ResolveError(
            f"base {base_short} of fix {short} is not in the local repository (a shallow "
            "clone?); fetch more history, or let RepoRewind fetch it by leaving out --repo-dir"
        )
    base_info = git.commit_info(base_sha)
    patches = parse_patch(git.diff(base_info.sha, fix_info.sha))
    if not patches:
        raise ResolveError(f"fix {short} does not change any file relative to base {base_short}")
    split = split_patches(patches, rules)
    if not split.source:
        raise ResolveError(
            f"fix {short} only changes test files ({len(split.tests)}); there is no source "
            "change to verify"
        )
    if not split.tests:
        raise ResolveError(
            f"fix {short} changes no test files, so it cannot prove a fail-to-pass flip; "
            "if its tests live elsewhere, adjust the split rules (test dirs, include globs)"
        )
    proof = prove_split(git, base_info.sha, fix_info.sha, split)
    resolved = ResolvedFix(
        repo=repo,
        fix=fix_info,
        base=base_info,
        source_patches=split.source,
        test_patches=split.tests,
        pr_number=pr_number,
    )
    return Resolution(fix=resolved, proof=proof)


__all__ = ["Resolution", "SplitProof", "prove_split", "resolve_fix", "select_base"]
