#!/bin/sh
# End-to-end RepoRewind demo on the bundled sample repository.
#
# Offline and self-contained: it imports demo/slugkit.fi (a git fast-import
# stream with pinned commit SHAs) into a scratch bare repository, resolves
# three commits with `reporewind resolve`, and then checks the fail-to-pass
# flip of the first fix by hand with git and unittest (the automated verify
# stage is on the roadmap).
#
# Environment:
#   REPOREWIND            command that runs the CLI      (default: reporewind)
#   PYTHON                interpreter with reporewind    (default: python3)
#   REPOREWIND_DEMO_WORK  keep outputs in this directory (default: a temp dir, removed)
set -eu

DEMO_DIR=$(cd "$(dirname "$0")" && pwd)
REPOREWIND=${REPOREWIND:-reporewind}
PYTHON=${PYTHON:-python3}
if [ -n "${REPOREWIND_DEMO_WORK:-}" ]; then
    WORK=$REPOREWIND_DEMO_WORK
    mkdir -p "$WORK"
else
    WORK=$(mktemp -d "${TMPDIR:-/tmp}/reporewind-demo.XXXXXX")
    trap 'rm -rf "$WORK"' EXIT
fi
REPO=$WORK/slugkit.git
START=$(date +%s)

# Same hermetic git settings RepoRewind uses: no user or system config.
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 PYTHONDONTWRITEBYTECODE=1

step() { printf '\n== %s\n' "$*"; }

step "1/5 import the bundled sample repository (pinned SHAs, no network)"
git init --quiet --bare --initial-branch=main "$REPO"
git -C "$REPO" fast-import --quiet <"$DEMO_DIR/slugkit.fi"
git -C "$REPO" log --graph --oneline --decorate=short main

step "2/5 resolve a plain fix commit"
$REPOREWIND resolve example/slugkit fix-separators --repo-dir "$REPO" \
    -o "$WORK/fix-separators.json"

step "3/5 resolve a merged pull request (merge commit, mainline 1)"
$REPOREWIND resolve example/slugkit merge-unicode --mainline 1 --repo-dir "$REPO" \
    -o "$WORK/merge-unicode.json"

step "4/5 a commit without test changes cannot prove a flip and is rejected"
set +e
$REPOREWIND resolve example/slugkit refactor-only --repo-dir "$REPO" --quiet >/dev/null
code=$?
set -e
if [ "$code" -ne 10 ]; then
    echo "expected exit code 10 (resolve error), got $code" >&2
    exit 1
fi
echo "exit code $code (resolve error), as expected"

step "5/5 check the fail-to-pass flip of fix-separators by hand"
BASE=$($PYTHON - "$WORK/fix-separators.json" "$WORK" <<'EOF'
import sys
from pathlib import Path

from reporewind.resolve import load_resolved_fix

fix = load_resolved_fix(Path(sys.argv[1]).read_text(encoding="ascii"))
out = Path(sys.argv[2])
for name, patches in (("fix.patch", fix.source_patches), ("test.patch", fix.test_patches)):
    data = "".join(p.diff for p in patches).encode("utf-8", "surrogateescape")
    (out / name).write_bytes(data)
print(fix.base.sha)
EOF
)
git -C "$REPO" worktree add --quiet --detach "$WORK/base" "$BASE"
cd "$WORK/base"
git apply "$WORK/test.patch"
if PYTHONPATH=src $PYTHON -m unittest discover -s tests 2>"$WORK/before.log"; then
    echo "base + test.patch unexpectedly passed" >&2
    exit 1
fi
echo "base + test.patch:             $(tail -n 1 "$WORK/before.log")"
git apply "$WORK/fix.patch"
PYTHONPATH=src $PYTHON -m unittest discover -s tests 2>"$WORK/after.log"
echo "base + fix.patch + test.patch: $(tail -n 1 "$WORK/after.log")"

printf '\ndemo finished in %ss\n' "$(($(date +%s) - START))"
