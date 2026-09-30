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

git submodule update --init -- upstream/honcho
git -C upstream/honcho fetch origin --tags --prune
target_tag="${1:-}"
if [[ -z "$target_tag" ]]; then
  target_tag="$(git -C upstream/honcho tag --list 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)"
fi
if [[ ! "$target_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "error: expected a stable vX.Y.Z release tag" >&2
  exit 1
fi
if ! git -C upstream/honcho rev-parse --verify --quiet "refs/tags/$target_tag" >/dev/null; then
  echo "error: official tag not found: $target_tag" >&2
  exit 1
fi
current_tag="$(tr -d '[:space:]' < .honcho-upstream-version)"
if [[ "$target_tag" == "$current_tag" ]]; then
  echo "Already based on the latest selected release: $target_tag"
  exit 0
fi
if ! git -C upstream/honcho merge-base --is-ancestor "$current_tag" "$target_tag"; then
  echo "error: $target_tag is not a descendant of current base $current_tag" >&2
  exit 1
fi

candidate_branch="integration/$target_tag"
candidate_path="$repo_root/.worktrees/$target_tag"
if git show-ref --verify --quiet "refs/heads/$candidate_branch" || [[ -e "$candidate_path" ]]; then
  echo "error: candidate already exists: $candidate_path" >&2
  exit 1
fi
git worktree add -b "$candidate_branch" "$candidate_path" main
git -C "$candidate_path" submodule update --init -- upstream/honcho
git -C "$candidate_path/upstream/honcho" fetch origin --tags
target_commit="$(git -C "$candidate_path/upstream/honcho" rev-parse "$target_tag^{commit}")"
git -C "$candidate_path/upstream/honcho" checkout --detach "$target_commit"
node --input-type=module - "$candidate_path" "$target_tag" "$target_commit" <<'JS'
import fs from "node:fs";
import path from "node:path";
const [root, ref, commit] = process.argv.slice(2);
const file = path.join(root, "selfhost-source.json");
const manifest = JSON.parse(fs.readFileSync(file, "utf8"));
manifest.upstream.ref = ref;
manifest.upstream.commit = commit;
fs.writeFileSync(file, `${JSON.stringify(manifest, null, 2)}\n`);
fs.writeFileSync(path.join(root, ".honcho-upstream-version"), `${ref}\n`);
JS
git -C "$candidate_path" add upstream/honcho selfhost-source.json .honcho-upstream-version
if ! node "$candidate_path/scripts/prepare-source.mjs"; then
  echo "Patch failure is isolated in: $candidate_path" >&2
  echo "Repair patches there, rerun prepare-source.mjs and tests, and commit the candidate." >&2
  exit 1
fi
git -C "$candidate_path" commit -m "chore(local): pin upstream $target_tag"
git -C "$candidate_path" diff --check
echo "Prepared $target_tag in $candidate_path"
echo "Validate the candidate, then use scripts/promote_upstream_update.sh $target_tag"
