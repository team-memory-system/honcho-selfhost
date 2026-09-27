# Local Honcho release workflow

This checkout keeps the self-hosted deployment as a maintained merge of an
official Honcho release and a small set of local commits.

## Branches and remotes

- `upstream`: the official `plastic-labs/honcho` repository. Its push URL is
  deliberately disabled.
- `origin`: the private self-host repository used to back up and publish local
  commits. `main` tracks `origin/main`, never `upstream/main`.
- `main`: the production source branch with local commits.
- `upstream-base/vX.Y.Z`: immutable pointers to official releases used by the
  local deployment.
- `integration/vX.Y.Z`: temporary update candidates created in an isolated Git
  worktree.

`.honcho-upstream-version` records the official release currently merged into
`main`.

## What belongs in Git

Keep local changes as narrow, independently revertible commits:

1. Self-host companion applications (dashboard and local MCP
   bridge).
2. Honcho core extensions that still differ from upstream.
3. Operational migration tools.
4. This release workflow.

Runtime state does not belong in Git: `.env`, API keys, database data, MCP tool
state, logs, generated dependencies, backups, and LaunchAgent-local secrets.

The local MCP bridge source lives in `local-mcp-bridge/`. Its host-specific
tool state belongs in `~/.config/honcho/mcp-bridge/`, and its logs belong in
`~/Library/Logs/Honcho/`. The official upstream `mcp/` directory is a separate
Cloudflare Worker and must not absorb the local Python bridge.

## Updating Honcho

Prepare the newest stable release without touching the production checkout:

```bash
scripts/prepare_upstream_update.sh
```

Or target an explicit official tag:

```bash
scripts/prepare_upstream_update.sh v3.0.12
```

The command fetches official history, creates `.worktrees/vX.Y.Z` from
`main`, and merges the new tag there. Resolve any conflicts and commit
them inside that worktree. Git `rerere` is enabled, so recurring resolutions
are remembered.

Validate the candidate in its worktree before promotion:

```bash
cd .worktrees/vX.Y.Z
uv sync --frozen
uv run ruff check src tests
uv run basedpyright src
uv run pytest -q
docker compose build api deriver
```

Database-dependent tests should use an isolated test database. Never point the
candidate test suite at the production database.

After validation, promote the already-tested candidate without deploying it:

```bash
scripts/promote_upstream_update.sh vX.Y.Z
```

Promotion creates a backup branch, fast-forwards `main`, and removes the
temporary worktree. Building and restarting production remains a separate,
explicit operation with the normal database backup and rollback checks.

## Why merge releases instead of stashing

Stashes are temporary, omit untracked files unless explicitly requested, and
do not preserve the intent of separate custom features. Release merges keep an
auditable history, use the true common ancestor even after many skipped
versions, and allow the full candidate to be tested without modifying the live
checkout.

## Independent LLM proxy (2026-09-23)

Codex and Claude proxy sources and LaunchAgents moved to `../llm-proxy`.
Honcho consumes their OpenAI-compatible HTTP endpoints; it does not package or
manage their lifecycle. Existing ports and private API keys remain unchanged.
Use `python3 ../llm-proxy/proxyctl.py status` to inspect the host services.
