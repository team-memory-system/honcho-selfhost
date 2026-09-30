#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 vX.Y.Z" >&2
  exit 1
fi

target_tag="$1"
repo_root="$(git rev-parse --show-toplevel)"
candidate_branch="integration/$target_tag"
candidate_path="$repo_root/.worktrees/$target_tag"
cd "$repo_root"

if [[ "$(git branch --show-current)" != "main" ]]; then
  echo "error: promotion must run from the main production checkout" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "error: main must be clean before promotion" >&2
  exit 1
fi
if ! git show-ref --verify --quiet "refs/heads/$candidate_branch"; then
  echo "error: candidate branch not found: $candidate_branch" >&2
  exit 1
fi
if [[ ! -d "$candidate_path" ]]; then
  echo "error: candidate worktree not found: $candidate_path" >&2
  exit 1
fi
if [[ -n "$(git -C "$candidate_path" status --porcelain)" ]]; then
  echo "error: candidate worktree has uncommitted changes" >&2
  exit 1
fi
if [[ "$(tr -d '[:space:]' < "$candidate_path/.honcho-upstream-version")" != "$target_tag" ]]; then
  echo "error: candidate does not record upstream $target_tag" >&2
  exit 1
fi
if ! git merge-base --is-ancestor main "$candidate_branch"; then
  echo "error: candidate cannot fast-forward main" >&2
  exit 1
fi

backup_branch="backup/custom-before-${target_tag}-$(date +%Y%m%d%H%M%S)"
git branch "$backup_branch" main
git merge --ff-only "$candidate_branch"
git submodule update --init -- upstream/honcho
node scripts/prepare-source.mjs
git -C "$candidate_path" submodule deinit --force -- upstream/honcho
git worktree remove --force "$candidate_path"
git branch -d "$candidate_branch"

echo "Promoted $target_tag to main."
echo "Rollback branch: $backup_branch"
echo "Production has not been rebuilt or restarted."
