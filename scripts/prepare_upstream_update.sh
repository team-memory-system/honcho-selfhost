#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

if [[ "$(git branch --show-current)" != "main" ]]; then
  echo "error: run this from the main production checkout" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "error: main must be clean before preparing an update" >&2
  exit 1
fi

git fetch upstream --tags --prune

target_tag="${1:-}"
if [[ -z "$target_tag" ]]; then
  target_tag="$({ git tag --list 'v*'; } | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)"
fi

if ! git rev-parse --verify --quiet "refs/tags/$target_tag" >/dev/null; then
  echo "error: official tag not found: $target_tag" >&2
  exit 1
fi

current_tag="$(tr -d '[:space:]' < .honcho-upstream-version)"
if [[ "$target_tag" == "$current_tag" ]]; then
  echo "Already based on the latest selected release: $target_tag"
  exit 0
fi

if ! git merge-base --is-ancestor "$current_tag" "$target_tag"; then
  echo "error: $target_tag is not a descendant of current base $current_tag" >&2
  exit 1
fi

candidate_branch="integration/$target_tag"
candidate_path="$repo_root/.worktrees/$target_tag"
if git show-ref --verify --quiet "refs/heads/$candidate_branch"; then
  echo "error: candidate branch already exists: $candidate_branch" >&2
  exit 1
fi
if [[ -e "$candidate_path" ]]; then
  echo "error: candidate path already exists: $candidate_path" >&2
  exit 1
fi

git worktree add -b "$candidate_branch" "$candidate_path" main

set +e
git -C "$candidate_path" merge --no-ff --no-edit "$target_tag"
merge_status=$?
set -e

if [[ $merge_status -ne 0 ]]; then
  echo
  echo "Merge conflicts are isolated in: $candidate_path"
  echo "Resolve them there, then run:"
  echo "  git -C '$candidate_path' add -A"
  echo "  git -C '$candidate_path' commit"
  echo "  printf '%s\\n' '$target_tag' > '$candidate_path/.honcho-upstream-version'"
  echo "  git -C '$candidate_path' add .honcho-upstream-version"
  echo "  git -C '$candidate_path' commit -m 'chore(local): record upstream $target_tag'"
  exit $merge_status
fi

printf '%s\n' "$target_tag" > "$candidate_path/.honcho-upstream-version"
git -C "$candidate_path" add .honcho-upstream-version
git -C "$candidate_path" commit -m "chore(local): record upstream $target_tag"
git -C "$candidate_path" diff --check

echo
echo "Prepared update candidate:"
echo "  branch:   $candidate_branch"
echo "  worktree: $candidate_path"
echo
echo "Validate it there, then promote with:"
echo "  scripts/promote_upstream_update.sh $target_tag"
