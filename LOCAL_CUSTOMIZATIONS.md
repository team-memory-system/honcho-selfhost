# Local Honcho source and release workflow

Official Honcho is the pristine `upstream/honcho` Git submodule. This repository
tracks its exact commit, our core patch set, and independent companion services.
It no longer merges the official source tree into its root.

## Source identity

`selfhost-source.json` records the official repository/ref/commit, ordered patches,
and local companion paths. Its upstream commit must match the Git submodule; its
ref must match `.honcho-upstream-version`. `patches/0001-selfhost-core.patch`
contains the former fork differences in `src/` and `tests/` only.

`node scripts/prepare-source.mjs` exports the official commit with `git archive`,
checks/applies patches in an isolated directory, adds tracked companion files,
and records the resulting provenance in `.honcho-source.json`. The output defaults
to `.build/honcho`; `--output <directory>` is used by the installer and release
builder. Only output previously owned by this preparer can be replaced. A failure
preserves the last successful output and never changes the upstream submodule.

## Adopting an official release

1. Run `scripts/prepare_upstream_update.sh vX.Y.Z` from clean `main`.
2. The candidate worktree updates its official gitlink, source manifest, and
   version file. Its patch application must succeed; repair the patch in that
   candidate when upstream changed the same code.
3. Run source preparation and tests against an isolated test database. Commit the
   candidate and run `scripts/promote_upstream_update.sh vX.Y.Z`.
4. Build/deploy separately after the usual database backup and runtime checks.

The existing Git history is retained. The source before this layout change is
`4282bd243d925d8785a004afc13b8eb04eede3df`. During migration, 1,126 prepared source
files matched that revision byte for byte; wrapper docs and deployment metadata
were deliberately excluded from that source comparison.

## Configuration and installation

Runtime `.env`, credentials, database volumes, logs, and generated source remain
ignored. The checked-in Compose file builds from `.build/honcho`; the API and
worker use the upstream Dockerfile. The installer materializes this wrapper into
the former flat runtime layout, so installed Compose paths remain stable.

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

The Jev gate is off unless `HONCHO_JEV_GATE` is set. With `HONCHO_JEV_GUARD_URL`
(a Team Memory member's server, whose app writes it with `HONCHO_JEV_GUARD_TOKEN`)
it posts each gated query, as `{tool, caller, workspace, query}`, to the team
hub's `/guard` with that server's token and follows the hub's verdict: the team's
Jev key, model and threshold live in the hub, and a hub with no key answers
`judged: false`, which lets the call through unjudged. Without it, it calls Jev
itself through `typesafe_sdk` with `TYPESAFE_API_KEY`, imported on first use.
Either way the gate reads the whole query, not the audit log's 8000-character
copy, `HONCHO_JEV_FAIL_MODE` decides whether an outage, or a refusal of the
token, forwards the query or refuses it, and a query forwarded without a judgment
keeps the reason on its audit row.

## Independent LLM proxy (2026-09-23, router since 2026-09-30)

Codex and Claude proxy sources moved out of this repository on 2026-09-23. They
now live in `../subscription-gateway` (the repository was named `llm-proxy`).
Honcho consumes an OpenAI-compatible HTTP endpoint. It does not package the
gateway or manage its lifecycle.

Since 2026-09-30 every chat model setting in `.env` points at the gateway's
router, `http://host.docker.internal:11400/v1`, and `LLM_VLLM_API_KEY` holds the
router's client key (`router` in
`~/Library/Application Support/SubscriptionGateway/secrets.json`). The router
sends each request to a logged-in account and moves to the next account when one
hits its usage limit. The LaunchAgent `subscription-gateway.ui` (screen on 11450)
starts the router and the per-account adapters and restarts them if they stop.
Embeddings are unchanged.

The single-account proxy Honcho used before, `com.chenjing.llm-proxy.codex`
(11435), was retired, as was its Claude sibling `com.chenjing.llm-proxy.claude`
(11446). Their plists are in `~/Library/LaunchAgents-retired-20260930/`. To go
back:

1. restore `.env.bak-20260930-before-router` over `.env`;
2. move `com.chenjing.llm-proxy.codex.plist` back to `~/Library/LaunchAgents/`
   and `launchctl bootstrap gui/$(id -u)` it;
3. recreate `api` and `deriver` without `--build`.

Before switching, the router answered the request shapes Honcho sends (plain,
`parse` with a Pydantic schema, tools, and streaming with tools and
`stream_options`) the same way 11435 did.

## Special-token text is plain text (2026-09-28)

tiktoken refuses by default to encode the literal text of a special token
(`<|endoftext|>`, `<|fim_prefix|>`, …) and raises `ValueError`. Honcho encodes
message content to count and chunk it, so a transcript that merely mentions one
of those strings returned HTTP 500 on every store attempt and stalled the
collector queue behind it. `src/tiktoken_plain_text.py` patches
`tiktoken.Encoding.encode` once to default `disallowed_special=()`; it is
imported from `src/embedding_client.py`, which every API and deriver path loads
and which already differs from upstream. Upstream call sites are unchanged.
`tests/test_tiktoken_plain_text.py` covers it.
