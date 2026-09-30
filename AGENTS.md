# Working in this fork

`README.md` and `CLAUDE.md` in this repository are upstream's. This file is local,
and it is the one to read first.

## What this is

A maintained fork of [`plastic-labs/honcho`](https://github.com/plastic-labs/honcho),
AGPL-3.0, run as one person's private memory server. `.honcho-upstream-version`
records which official release the fork is currently based on.

The memory system uses two Team Memory System repositories and an independently
maintained gateway:

| Repository | What it is | Installed where |
|---|---|---|
| **`honcho-selfhost`** (this one) | The memory server, plus the MCP bridge and dashboard | One computer per person |
| [`honcho-agent-bridge`](https://github.com/team-memory-system/honcho-agent-bridge) | Collector, installer, agent plugin | Every machine that runs an agent |
| [`subscription-gateway`](https://github.com/chenjingdev/subscription-gateway) | Independent subscription-to-API gateway: an adapter per account and a router that fails over between them. The agent bridge installs a pinned source revision | The computer that runs Honcho |

**Topology.** One Honcho and one database per person; that person's several machines
all feed the same one. Teammates do not share a database. What is shared is a single
MCP tool, `chat`, served by a second bridge process with its own narrowed tool list.

## The rule that shapes everything here

**Upstream keeps moving, and this fork keeps merging it.** So every local change is
written to be as small and as revertible as possible, and to touch files upstream is
unlikely to touch. `LOCAL_CUSTOMIZATIONS.md` enumerates what differs and why.

Two worked examples of what that discipline buys:

- The retrieval-instruction feature used to add `embed_query()` and change 13 call
  sites. Those 13 files then conflicted on every merge. It now reads the
  `embedding_call_purpose` ContextVar that upstream already sets, inside `_prepare()`,
  so the call sites are upstream's own code. Merging the next release touches
  **two** files instead of fourteen.
- The audit log lives in its own database schema, created on first write. `DB.SCHEMA`
  being configurable is what makes that safe: no alembic migration ever sees the
  table, so it costs nothing at merge time and Honcho's own source is unchanged.

### Merging a new release

Use the script. Do not merge a tag directly on `main`.

```sh
scripts/prepare_upstream_update.sh          # newest official tag
scripts/prepare_upstream_update.sh v3.2.1   # a specific one
```

It refuses a dirty `main`, refuses a tag that is not a descendant of the recorded
base, and does the merge in a throwaway worktree under `.worktrees/` so the
production checkout is never mid-merge. `rerere` is on, so a resolution you make
once is reapplied. Promote with `scripts/promote_upstream_update.sh`.

## Running it

```sh
docker compose -f docker-compose.selfhost.yml up -d
```

- `docker-compose.selfhost.yml` is **tracked** and describes the whole stack: api,
  deriver, database, redis, two MCP bridges, dashboard.
- `docker-compose.yml` is in `.gitignore` and only ever described one machine. The
  tracked file is deliberately named so `docker compose` never auto-loads it; pass
  `-f`.
- `HONCHO_CONFIG_DIR` has no default and must be set, in the `.env` beside the
  compose file. Compose evaluates a nested default eagerly, so falling back to the
  home directory would still demand a variable Windows does not have. Unset, the
  command stops and says so.

**Never pass `--build` unless you mean to ship the current working tree to the
running system.** The images are built from this checkout. A rebuild while something
is uncommitted deploys that something.

## The MCP bridge

`local-mcp-bridge/server.py`. Imports zero Honcho code — it is an HTTP client — which
is why it can live here at no merge cost. 31 tools.

Everything that matters hangs off one function, `register_tool`:

| Concern | Where |
|---|---|
| Audit log, with the query text | `audit.py` |
| Refusing a query before it reaches Honcho | `jev_gate.py` |
| Runtime tool toggles from the dashboard | `_currently_disabled()` |

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

- **Auth is off.** `AUTH_USE_AUTH=false`, so `src/security.py` returns admin for every
  request. Turning it on is four simultaneous edits (this `.env`, the REST proxy, the
  bridge, the collector's environment) and will break live collection if done partly.
- **`TRUSTED_HOSTS` was removed and should probably go back.** See the commit
  `revert(security)`. Upstream has no such middleware, so removing it only reduced
  merge surface — but it was the only DNS-rebinding defence while a REST endpoint is
  reachable from outside, and no replacement was added. The release builder still
  emits a `TRUSTED_HOSTS` setting that nothing reads.
- **Based on `v3.2.1` since 2026-09-30.** The merge had three mechanical conflict
  hunks: the `simple_batch_embed` call in `src/embedding_client.py` (upstream's
  `on_oversize` plus this fork's `_prepare`), and import blocks there and in the
  matching test. The fork's `RecordingInnerClient` test fake had to accept
  `on_oversize` too. Deployed the same day; migration `a7c3e9f1b2d4`
  (`document_sources`) ran, and the reconciler's new `backfill_document_sources`
  task fills that table in the background.
- **Testing a candidate from `.worktrees/`.** `src/config.py` calls
  `load_dotenv(override=True)`, which walks up to the production checkout's `.env`
  and overrides the environment. Run the suite with `PYTHON_DOTENV_DISABLED=1`,
  `DB_CONNECTION_URI` pointing at a throwaway `pgvector/pgvector:pg15` container, and
  that container started with `POSTGRES_HOST_AUTH_METHOD=trust` (the conftest renders
  the URL with the password masked, as upstream CI does). The TypeScript SDK tests
  also need `bun install` in `sdks/typescript`; the lancedb and qdrant tests need
  `uv sync --all-extras`. basedpyright reports 20 errors in upstream's own
  `src/vector_store/lancedb.py` and `qdrant.py` at `v3.2.1`; this install uses
  pgvector.
- **The two bridges need different bearer tokens.** They read
  `HONCHO_MCP_BEARER_TOKEN_FILE`, and `scripts/write_bridge_secrets.sh` writes both
  from 1Password. If one value is used for both, whoever holds the teammates'
  token can also call the owner's bridge, so check that before handing one out.

## Verify a change

```sh
uv run pytest tests/llm tests/test_security.py -q      # no database needed
cd local-mcp-bridge  && uv run pytest -q && uv run ruff check .
cd local-dashboard   && npm test
```

Database-backed suites need the `database` host name from inside Compose; they do not
run from the host with this `.env`.

Two tests exist to fail loudly rather than degrade quietly:
`test_query_purposes_partition_upstream_taxonomy` (an upstream purpose nobody
classified) and `test_exactly_the_query_paths_route_through_prepare` (the wiring
being removed while the taxonomy still looks right).

## Secrets

Never in this repository. `.env` is ignored. The bridge reads its bearer token from a
file (`scripts/write_bridge_secrets.sh` / `.ps1` populate it from 1Password), so no
token sits in a plist, a compose file, or `docker inspect`.
