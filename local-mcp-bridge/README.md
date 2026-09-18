# Honcho local MCP bridge

This companion service exposes the self-hosted Honcho v3 API as a streamable
HTTP MCP server. It is local customization source tracked on `custom/main`; it
is not part of the official upstream `mcp/` Cloudflare Worker.

## Runtime path

The tracked source and host-specific runtime state are deliberately separate:

```text
/Users/chenjing/dev/honcho/local-mcp-bridge/  # Git-tracked source
~/.config/honcho/mcp-bridge/                 # tool state and optional token
~/Library/Logs/Honcho/                       # service logs
```

The production-compatible endpoint remains `http://127.0.0.1:8766/mcp`, under
the existing `com.chenjing.honcho-external-mcp` LaunchAgent label. Keeping the
label, port, and bearer value stable allows existing Codex and tunnel clients
to survive a source-path migration.

The current Codex identity is carried in request headers:

- workspace: `memory`
- user peer: `user_chen`
- assistant peer: `assistant_codex`

## Install and test

```bash
cd /Users/chenjing/dev/honcho/local-mcp-bridge
uv sync --frozen
uv run ruff check server.py tests
uv run pytest -q
```

For a local, unauthenticated development instance bound only to loopback:

```bash
uv run python server.py
```

## Production safety

The externally tunneled bridge must set all of the following:

```text
HONCHO_MCP_BEARER_TOKEN or HONCHO_MCP_BEARER_TOKEN_FILE
HONCHO_MCP_REQUIRE_AUTH=1
HONCHO_MCP_TOOL_CONFIG=~/.config/honcho/mcp-bridge/tool-config.json
HONCHO_MCP_REQUIRE_TOOL_CONFIG=1
HONCHO_MCP_TIMEOUT=300
```

When either required auth or required tool configuration is missing or invalid,
the process exits instead of exposing an unauthenticated or unexpectedly broad
tool set. Direct bearer-token environment variables take precedence over a
token file. Never commit a live token, Cloudflare credential, LaunchAgent with
inline secrets, or dashboard-generated tool state.

Seed a new runtime tool configuration from
`examples/tool-config.read-only.json`, then keep the live file outside Git with
mode `0600`. The example disables every current create, update, delete, dream,
and operational-inspection tool that is disabled in production.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `HONCHO_BASE_URL` | `http://127.0.0.1:8001` | Honcho v3 API |
| `HONCHO_WORKSPACE_ID` | `memory` | default workspace |
| `HONCHO_USER_NAME` | `user_chen` | default user peer |
| `HONCHO_ASSISTANT_NAME` | `assistant` | default assistant peer |
| `HONCHO_MCP_HOST` | `127.0.0.1` | listener host |
| `HONCHO_MCP_PORT` | `8765` | listener port; production overrides to `8766` |
| `HONCHO_MCP_PATH` | `/mcp` | streamable HTTP path |
| `HONCHO_MCP_TIMEOUT` | `300` | Honcho upstream HTTP timeout in seconds |
| `HONCHO_MCP_TOOL_CONFIG` | XDG config path | dashboard-managed tool state |
| `HONCHO_MCP_BEARER_TOKEN` | empty | direct MCP bearer secret |
| `HONCHO_MCP_BEARER_TOKEN_FILE` | empty | repo-external bearer file |
| `HONCHO_MCP_REQUIRE_AUTH` | false | fail startup when bearer is unavailable |
| `HONCHO_MCP_REQUIRE_TOOL_CONFIG` | false | fail startup when tool state is unavailable |
| `HONCHO_MCP_HIDE_PEER_CARDS` | false | omit card tools and reject card inclusion in session context for this process |

`X-Honcho-Workspace-ID`, `X-Honcho-User-Name`, and
`X-Honcho-Assistant-Name` request headers override the corresponding defaults.

## Separate coworker endpoint

The dedicated `com.chenjing.honcho-coworker-mcp` LaunchAgent listens on
`127.0.0.1:8767`, using its own tool configuration in
`~/.config/honcho/coworker-mcp/tool-config.json`. Its public MCP URL is
`https://honcho-coworker.chenjing.org/mcp`.

This process sets `HONCHO_MCP_HIDE_PEER_CARDS=1`. The card tools are not
registered, and session-context requests specifying `peer_target` are rejected.
Ordinary session context, search, and representation remain available. Request
headers cannot turn card tools back on. Search and representation still return
personal information; this configuration restricts card endpoints, not facts.

Cloudflare Access application `honcho-mcp-coworker` protects the new hostname.
The existing coworker service token is accepted there; the owner's original
MCP and API hosts retain their registered-device policy. Existing credentials
remain valid at the new URL. The owner's bridge has no coworker-header rule.

To migrate an existing client, change only the `honcho_chen` server URL to the
new URL, preserve its authentication headers, and restart the client from a
shell with its existing `HONCHO_CHEN_*` environment variables loaded. Verify
using `get_representation(peer_id="user_chen", observer_id="user_chen")`.

## Upstream maintenance

This directory is an independently revertible local companion commit. Official
Honcho releases continue to arrive through the repository's existing
`integration/vX.Y.Z` worktree and `custom/main` promotion workflow described in
`../LOCAL_CUSTOMIZATIONS.md`. Do not move this bridge into upstream-owned
`../mcp/`.
