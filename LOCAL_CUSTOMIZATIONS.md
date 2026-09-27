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
`~/Library/Logs/Honcho/`.

The upstream `mcp/` directory is not a substitute for it. As of upstream
`2ad56a4d` (2026-09-02) `mcp/` is self-hostable — it has `src/http.ts` on
`Bun.serve`, a `Dockerfile`, and an `mcp:` service in
`docker-compose.yml.example` — so "it is only a Cloudflare Worker" is no longer
the reason to keep them apart. The reason is what `local-mcp-bridge/server.py`
does that upstream's does not: it narrows the tool list per caller
(`HONCHO_MCP_ENABLED_TOOLS`), ignores the `x-honcho-*` headers on a shared
bridge (`HONCHO_MCP_PIN_DEFAULTS`), records every call with its query text
(`audit.py`), and can refuse a query before it reaches Honcho
(`jev_gate.py`). Those live in `register_tool`, which is the single place every
one of its tools passes through. The two can run side by side.

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

## Reproducible stack (2026-09-28)

`docker-compose.yml` is in `.gitignore` and only ever described one machine.
`docker-compose.selfhost.yml` is tracked and holds the whole stack: the API, the
deriver, the database, the cache, both MCP bridges and the dashboard. It is named
so that `docker compose` never auto-loads it — pass it with `-f`.

Two bridges run from one image. `mcp-bridge` is the owner's, with the tool set
the dashboard controls and the audit read enabled. `mcp-bridge-shared` is what
teammates reach: `HONCHO_MCP_ENABLED_TOOLS=chat`, pinned defaults, no audit read.

The audit log lives in its own schema (`HONCHO_AUDIT_SCHEMA`, default
`honcho_audit`) inside the Honcho database, created on first write. `DB.SCHEMA`
being configurable is what makes that safe: no alembic migration ever sees it,
so it costs nothing at merge time and Honcho's own source is unchanged.

The Jev gate is off unless `HONCHO_JEV_GATE` is set. It uses `typesafe_sdk`
directly, imported on first use, and `HONCHO_JEV_FAIL_MODE` decides whether a
Jev outage forwards the query or refuses it.

## Independent LLM proxy (2026-09-23)

Codex and Claude proxy sources and LaunchAgents moved to `../llm-proxy`.
Honcho consumes their OpenAI-compatible HTTP endpoints; it does not package or
manage their lifecycle. Existing ports and private API keys remain unchanged.
Use `python3 ../llm-proxy/proxyctl.py status` to inspect the host services.
