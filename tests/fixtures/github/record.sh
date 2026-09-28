#!/usr/bin/env bash
# Re-record the GitHub REST API fixtures used by tests/test_resolve_github.py.
#
# Each response is fetched from the live API and trimmed with jq to the
# fields RepoRewind reads, so the fixtures stay small and the filter below
# documents exactly what the client depends on. Needs curl and jq; set
# GITHUB_TOKEN to avoid the anonymous rate limit.
#
#   tests/fixtures/github/record.sh
set -euo pipefail

cd "$(dirname "$0")"
API=https://api.github.com
AUTH=()
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  AUTH=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
fi

PULL='{number, title, state, merged, merged_at, merge_commit_sha, commits,
       base: {sha: .base.sha, ref: .base.ref}, head: {sha: .head.sha, ref: .head.ref}}'
COMMIT='{sha, parents: [.parents[] | {sha}],
         commit: {message: .commit.message, author: {date: .commit.author.date}}}'

get() { # <api path> <jq filter> <output file>
  mkdir -p "$(dirname "$3")"
  curl -fsSL --retry 3 --retry-all-errors "${AUTH[@]}" -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" "$API$1" | jq --sort-keys "$2" > "$3"
  echo "recorded $1 -> $3"
}

pull_merge_and_commits() { # <owner/repo> <number>
  local repo=$1 number=$2 merge commits pages
  get "/repos/$repo/pulls/$number" "$PULL" "repos/$repo/pulls/$number.json"
  merge=$(jq -r .merge_commit_sha "repos/$repo/pulls/$number.json")
  get "/repos/$repo/commits/$merge" "$COMMIT" "repos/$repo/commits/$merge.json"
  commits=$(jq -r .commits "repos/$repo/pulls/$number.json")
  pages=$(( (commits + 99) / 100 ))
  get "/repos/$repo/pulls/$number/commits?per_page=100&page=$pages" "[.[] | $COMMIT]" \
    "repos/$repo/pulls/$number/commits.page-$pages.json"
}

# A pull request landed with a merge commit (two parents).
pull_merge_and_commits pallets/markupsafe 477
# A two-commit pull request landed as one squashed commit.
pull_merge_and_commits python-attrs/attrs 1606
