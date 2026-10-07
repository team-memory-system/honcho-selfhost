# Working in this self-host wrapper

Read this file first. The official Honcho repository is the pinned submodule at
`upstream/honcho`; its README, CLAUDE.md, and skills describe upstream behavior.
The root README and this file describe our wrapper.

## What this is

`team-memory-system/honcho-selfhost` keeps official Honcho, our patches, and our
companion services separate. It remains AGPL-3.0.

- `upstream/honcho/`: pristine `plastic-labs/honcho`, pinned by the Git submodule.
- `selfhost-source.json`: the matching official ref/commit, patch order, and local
  paths included in a prepared source tree.
- `patches/`: the only place for Honcho core changes, including their tests.
- `local-mcp-bridge/`, `local-dashboard/`: our independent HTTP companion services.
- `.build/honcho/`: generated, ignored, patched runtime source. Never hand-edit it.

`honcho-agent-bridge` remains the collector/installer. It installs
`team-memory-system/subscription-gateway` through the gateway CLI. One Honcho and one DB
belong to each person; their several machines feed that same server.

## Updating or changing the core

Never edit the upstream submodule in place or merge upstream into this root repo.
Make core changes in a disposable export, regenerate the patch, and verify it.
`node scripts/prepare-source.mjs` exports the pinned official commit, applies the
patches in order, adds tracked companion files, and atomically replaces the
prepared tree. It refuses a dirty or incorrectly pinned upstream and leaves the
last output intact when a patch fails. The generated `.honcho-source.json` records
source commits and patch checksums.

For a new official release use:

```sh
scripts/prepare_upstream_update.sh v3.2.2
```

The script makes a candidate worktree, updates its submodule/manifest/version,
and replays the patches there. Resolve patch failures in that candidate, test,
and commit it before `scripts/promote_upstream_update.sh v3.2.2`. Promotion does
not rebuild or restart production.

## Running it

Requires Git, Node.js 18+, and Docker with Compose.

```sh
node scripts/prepare-source.mjs
docker compose -f docker-compose.selfhost.yml up -d --build
```

Compose builds the API and deriver from `.build/honcho` using the official
Dockerfile. The dashboard and MCP bridge have their own build contexts.
`database/init.sql` is a symlink to the prepared SQL, retained for the existing
database container's bind mount; new Compose runs use the prepared source path.
`HONCHO_CONFIG_DIR` must be set in the ignored `.env` beside the Compose file.
`docker-compose.yml` is an ignored machine-specific file, never the distribution.

**Building or restarting the production stack is a deployment.** For verification,
build a separately named image and use a disposable database/network. Never run
tests against the production database or copy its `.env` into a candidate.

## The MCP bridge

`local-mcp-bridge/server.py`. Imports zero Honcho code — it is an HTTP client — which
is why it can live here at no merge cost. 31 tools.

Everything that matters hangs off one function, `register_tool`:

| Concern | Where |
|---|---|
| Audit log, with the query text | `audit.py` |
| Refusing a query before it reaches Honcho | `jev_gate.py` |
| Runtime tool toggles from the dashboard | `_currently_disabled()` |
| A teammate's projects behind a team server's gate (`HONCHO_MCP_SCOPE_FROM_GATE`) | `_projects_asked()` |

Two processes run from one image:

| | Tools | Workspace and peers | Audit read |
|---|---|---|---|
| `mcp-bridge` | Whatever the dashboard leaves on | Caller's choice, by header or argument | Yes |
| `mcp-bridge-shared` | `HONCHO_MCP_ENABLED_TOOLS=chat` | Fixed: headers ignored, arguments naming others refused (`HONCHO_MCP_PIN_DEFAULTS`) | No |

The pin checks every argument listed in `PEER_ARGUMENTS`, plus `workspace_id`, and
refuses `filters` outright. `test_every_peer_argument_is_pinned` fails when a tool
gains a peer-naming argument that is not in that list.

In front of all of it, `BearerGate` refuses the MCP endpoint at the HTTP layer when
the bearer token is missing or wrong. `_require_auth` only runs once a tool reaches
Honcho, so before the gate a wrong token still completed `initialize` and listed the
tools, and a teammate's connection check reported success. `_require_auth` stays as
the second check.

An allowlist, not a denylist: a 30-name denylist silently widens every time upstream
adds a tool.

The audit log must never break a tool call. `audit.record` swallows everything, the
connection has a timeout and backs off after a failure, and with no DSN it does
nothing at all. The Jev gate is off unless `HONCHO_JEV_GATE` is set.

## Open items an agent should know about

- **Auth is off.** `AUTH_USE_AUTH=false`, so `upstream/honcho/src/security.py` returns admin for every
  request. Turning it on is four simultaneous edits (this `.env`, the REST proxy, the
  bridge, the collector's environment) and will break live collection if done partly.
- **`TRUSTED_HOSTS` was removed and should probably go back.** See the commit
  `revert(security)`. Upstream has no such middleware, so removing it only reduced
  merge surface — but it was the only DNS-rebinding defence while a REST endpoint is
  reachable from outside, and no replacement was added. The release builder still
  emits a `TRUSTED_HOSTS` setting that nothing reads.
- **Based on `v3.2.2` since 2026-10-03, deployed the same night.** The API
  container was created at 2026-10-03 00:31 KST, and the live alembic head is
  `b8d2f4a6c9e1`. The patch applied
  cleanly. The wiring guard flagged upstream's new
  `EmbeddingClient.truncate_to_token_limit` (#1255), which cuts an oversized search
  query before it is embedded. It is classified as token-only, and the fork's wrapper
  cuts with the query instruction already in place, so the prefixed query still fits
  the cap. Deploying ran a migration that adds a `session_peers (workspace_name,
  peer_name, session_name)` index, built without `CONCURRENTLY` (#1237). The previous
  base, `v3.2.1`, was deployed on 2026-09-30 with migration `a7c3e9f1b2d4`
  (`document_sources`), which the reconciler's `backfill_document_sources` task fills
  in the background.
- **A second, empty server runs on the DGX Spark** (`ssh spark`, `~/services/honcho`),
  built 2026-10-05. It runs the same v3.2.2 images, copied from this Mac, but embeds
  with Qwen3-Embedding 4B (`qwen3-embedding-4b-honcho-8192`, 1536 dimensions) on the
  GB10 instead of the 8B here. Its deriver reaches this Mac's LLM router through an
  ssh relay whose key may only forward to `127.0.0.1:11400`. It is meant to hold the
  memory rebuilt from the conversation originals; the live `memory` stays here until
  that cutover. Its `README.md` has the layout, the relay design and the throughput:
  4B on the GB10 embeds about 1,070 message-length texts a minute, 8B on this Mac
  about 114. Its compose file is specific to that host and is not in this repo.
- **Testing a prepared source tree.** Its `src/config.py` uses
  `load_dotenv(override=True)`, which can walk up to a production `.env`.
  Set `PYTHON_DOTENV_DISABLED=1` and use a throwaway pgvector database with
  `POSTGRES_HOST_AUTH_METHOD=trust`. The full suite also needs SDK dependencies
  and all vector-store extras; upstream v3.2.1 had 20 known basedpyright errors
  in its optional lancedb/qdrant files (not rechecked on v3.2.2). This installation uses pgvector.
- **The two bridges need different bearer tokens.** They read
  `HONCHO_MCP_BEARER_TOKEN_FILE`, and `scripts/write_bridge_secrets.sh` writes both
  from 1Password. If one value is used for both, whoever holds the teammates'
  token can also call the owner's bridge, so check that before handing one out.

## Verify a change

```sh
node --test tests/prepare-source.test.mjs
(cd .build/honcho && PYTHON_DOTENV_DISABLED=1 uv run pytest tests/llm tests/test_security.py -q)
(cd local-mcp-bridge && export UV_PROJECT_ENVIRONMENT="$(mktemp -d)" && uv run --frozen --all-extras pytest -q && uv run --frozen --all-extras ruff check .)
(cd local-dashboard && npm test)
```

Database-backed suites need the `database` host name from inside Compose; they do not
run from the host with this `.env`.

Run the MCP bridge's tests in a throwaway environment, as above. A plain `uv run`
syncs `local-mcp-bridge/.venv`, which is the interpreter of the live
`com.chenjing.honcho-external-mcp` LaunchAgent. Without `--all-extras`, 5 tests in
`tests/test_jev_gate.py` fail with `No module named 'typesafe_sdk'`.

Two tests exist to fail loudly rather than degrade quietly:
`test_query_purposes_partition_upstream_taxonomy` (an upstream purpose nobody
classified) and `test_exactly_the_query_paths_route_through_prepare` (the wiring
being removed while the taxonomy still looks right).

## Secrets

Never in this repository. `.env` is ignored. The bridge reads its bearer token from a
file (`scripts/write_bridge_secrets.sh` / `.ps1` populate it from 1Password), so no
token sits in a plist, a compose file, or `docker inspect`.
